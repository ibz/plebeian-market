import boto3
from botocore.config import Config
import click
from datetime import datetime, timedelta
from flask import Flask, jsonify, request, send_file
from flask.cli import with_appcontext
from flask_mail import Message
from flask_migrate import Migrate
from functools import wraps
import io
import json
import jwt
import logging
from logging.config import dictConfig
import magic
from nostr.event import Event, EncryptedDirectMessage
import os
import requests
from requests.exceptions import JSONDecodeError
import signal
from sqlalchemy import desc
import sys
import time
import uuid

from extensions import cors, db, mail

LOG_LEVEL = os.environ.get('LOG_LEVEL', 'DEBUG')

dictConfig({
    'version': 1,
    'formatters': {
        'default': {
            'format': "[%(asctime)s] %(levelname)s in %(module)s: %(message)s",
        }
    },
    'handlers': {'default': {
        'class': 'logging.StreamHandler',
        'formatter': 'default',
    }},
    'root': {
        'level': LOG_LEVEL,
        'handlers': ['default'],
    },
})

class MyFlask(Flask):
    def __init__(self, import_name, **kwargs):
        super().__init__(import_name, **kwargs)
        self.initialized = False

    def __call__(self, environ, start_response):
        if not self.initialized:
            from api import api_blueprint
            app.register_blueprint(api_blueprint)
            self.initialized = True
        return super().__call__(environ, start_response)

def create_app():
    app = MyFlask(__name__, static_folder="../web/static")
    app.config.from_object('config')
    cors.init_app(app)
    db.init_app(app)
    mail.init_app(app)
    return app

app = create_app()

import models as m

migrate = Migrate(app, db)

@app.cli.command("run-tests")
@with_appcontext
def run_tests():
    app.logger.setLevel(getattr(logging, LOG_LEVEL))
    signal.signal(signal.SIGTERM, lambda _, __: sys.exit(0))

    import unittest
    import api_tests
    suite = unittest.TestLoader().loadTestsFromModule(api_tests)
    unittest.TextTestRunner().run(suite)

@app.cli.command("finalize-auctions")
def finalize_auctions():
    app.logger.setLevel(getattr(logging, LOG_LEVEL))
    signal.signal(signal.SIGTERM, lambda _, __: sys.exit(0))

    app.logger.info("Starting finalize-auctions process...")

    while True:
        for auction in db.session.query(m.Auction).filter((m.Auction.end_date <= datetime.utcnow()) & (m.Auction.has_winner == None)):
            app.logger.info(f"Looking at {auction.id=}...")

            top_bid = auction.get_top_bid()

            while top_bid and db.session.query(m.Order).filter_by(buyer_public_key=top_bid.buyer_nostr_public_key).filter(m.Order.expired_at != None) \
            .join(m.OrderItem, m.OrderItem.order_id == m.Order.id).filter(m.OrderItem.auction_id == auction.id).first():
                app.logger.info(f"Skipping bidder {top_bid.buyer_nostr_public_key} with expired order for {auction.id=} and picking the next one!")
                top_bid = auction.get_top_bid(below=top_bid.amount)

            if not top_bid or not auction.reserve_bid_reached:
                app.logger.info(f"Auction {auction.id=} has no winner!")
                auction.has_winner = False
                auction.winning_bid_id = None
                db.session.commit()
                continue

            app.logger.info(f"Auction {auction.id=} has a winner: pubkey={top_bid.buyer_nostr_public_key}!")
            auction.has_winner = True
            auction.winning_bid_id = top_bid.id

            if top_bid.buyer_nostr_public_key:
                merchant = auction.item.seller
                if merchant.wallet:
                    try:
                        on_chain_address = merchant.get_new_address()
                    except m.AddressGenerationError:
                        app.logger.exception("Error while generating address.")
                        continue
                    except MempoolSpaceError:
                        app.logger.exception("Error while checking mempool.")
                        continue
                else:
                    on_chain_address = None
                lightning_address = merchant.lightning_address

                order_uuid = str(uuid.uuid4())

                birdwatcher = get_birdwatcher()

                if not birdwatcher.publish_bid_status(auction, top_bid.nostr_event_id, 'winner', extra_tags=[['p', top_bid.buyer_nostr_public_key]]):
                    continue

                dm_event_id = birdwatcher.send_dm(auction.item.seller.parse_merchant_private_key(), top_bid.buyer_nostr_public_key,
                    json.dumps({'id': order_uuid, 'type': 10, 'items': [{'product_id': str(auction.uuid), 'quantity': 1}]}))
                if not dm_event_id:
                    continue

                order = m.Order(
                    uuid=order_uuid,
                    seller_id=auction.item.seller_id,
                    buyer_public_key=top_bid.buyer_nostr_public_key,
                    requested_at=datetime.utcnow(),
                    on_chain_address=on_chain_address,
                    lightning_address=lightning_address,
                    event_id=dm_event_id)
                db.session.add(order)
                db.session.commit()

                order_item = m.OrderItem(order_id=order.id, item_id=auction.item_id, auction_id=auction.id, quantity=1)
                db.session.add(order_item)

            db.session.commit()

        if app.config['ENV'] == 'test':
            time.sleep(1)
        else:
            time.sleep(5)

@app.cli.command("settle-btc-payments")
@with_appcontext
def settle_btc_payments():
    app.logger.setLevel(getattr(logging, LOG_LEVEL))
    signal.signal(signal.SIGTERM, lambda _, __: sys.exit(0))

    btc = get_btc_client()

    app.logger.info(f"Starting to settle BTC payments using {type(btc)}...")

    while True:
        try:
            active_filter = (m.Order.paid_at == None) & (m.Order.expired_at == None) & (m.Order.canceled_at == None)
            for order in db.session.query(m.Order).filter((m.Order.on_chain_address != None) & active_filter):
                try:
                    funding_txs = btc.get_funding_txs(order.on_chain_address)
                except MempoolSpaceError as e:
                    app.logger.warning(str(e) + f" {order.on_chain_address=} Taking a 1 minute nap...")
                    time.sleep(60)
                    continue
                birdwatcher = get_birdwatcher()
                for tx in funding_txs:
                    if order.txid and not order.tx_confirmed:
                        if tx['confirmed'] and (tx['txid'] == order.txid or tx['value'] == order.tx_value):
                            if tx['txid'] != order.txid:
                                # this can happen in case of RBF
                                app.logger.info(f"Transaction txid={tx['txid']} differs from original txid={order.txid} matching {order.id=} but we still accept it.")
                                order.txid = tx['txid']
                            app.logger.info(f"Confirmed transaction txid={tx['txid']} matching {order.id=}.")
                            order.tx_confirmed = True
                            order.paid_at = datetime.utcnow()
                            if not birdwatcher.send_dm(order.seller.parse_merchant_private_key(), order.buyer_public_key,
                                json.dumps({'id': order.uuid, 'type': 2, 'paid': True, 'shipped': False, 'message': f"Payment confirmed. TxID: {order.txid}"})):
                                continue
                            db.session.commit()
                            break
                    elif not order.txid:
                        if tx['value'] >= order.total:
                            app.logger.info(f"Found transaction txid={tx['txid']} confirmed={tx['confirmed']} matching {order.id=}.")
                            order.txid = tx['txid']
                            order.tx_value = tx['value']
                            if tx['confirmed']:
                                order.tx_confirmed = True
                                order.paid_at = datetime.utcnow()
                                if not birdwatcher.send_dm(order.seller.parse_merchant_private_key(), order.buyer_public_key,
                                    json.dumps({'id': order.uuid, 'type': 2, 'paid': True, 'shipped': False, 'message': f"Payment confirmed. TxID: {order.txid}"})):
                                    continue
                            else:
                                message = f"Found transaction. Waiting for confirmation. TxID: {order.txid}"
                                if not birdwatcher.send_dm(order.seller.parse_merchant_private_key(), order.buyer_public_key,
                                    json.dumps({'id': order.uuid, 'type': 2, 'paid': False, 'shipped': False, 'message': message})):
                                    continue
                            db.session.commit()
                            break
                        else:
                            app.logger.warning(f"Found unexpected transaction when trying to settle {order.id=}: {order.total=} vs {tx['value']=}.")
                else:
                    if order.requested_at < datetime.utcnow() - timedelta(minutes=order.timeout_minutes):
                        app.logger.warning(f"Order too old. Marking as expired. {order.id=}")
                        order.expired_at = datetime.utcnow()
                        for order_item in db.session.query(m.OrderItem).filter_by(order_id=order.id):
                            # expired orders increment the stock with the quantity that was decremented when the order was created
                            # (for Listings, not for Auctions - auctions with expired orders are taken care of in finalize-auctions,
                            # where we detect the expired order and pick the next highest bidder as the new winner!)
                            if order_item.listing_id:
                                listing = db.session.query(m.Listing).filter_by(id=order_item.listing_id).first()
                                listing.available_quantity += order_item.quantity
                                birdwatcher.publish_product(listing) # TODO: what could we do here if this fails?
                        if not birdwatcher.send_dm(order.seller.parse_merchant_private_key(), order.buyer_public_key,
                            json.dumps({'id': order.uuid, 'type': 2, 'paid': False, 'shipped': False, 'message': "Order expired."})):
                            continue
                        db.session.commit()
                if order.paid_at and order.has_skin_in_the_game_donation_items():
                    for pending_bid in m.Bid.query.filter_by(buyer_nostr_public_key=order.buyer_public_key, settled_at=None).all():
                        app.logger.info(f"Confirmed bid {pending_bid.id} after having acquired Skin in the Game!")
                        pending_bid.settled_at = datetime.utcnow()
                        duration_extended = pending_bid.auction.extend()
                        birdwatcher.publish_bid_status(pending_bid.auction, pending_bid.nostr_event_id, 'accepted', duration_extended=duration_extended)
                        db.session.commit()
        except:
            app.logger.exception("Error while settling BTC payments. Will roll back and retry.")
            db.session.rollback()

        if app.config['ENV'] == 'test':
            time.sleep(1)
        else:
            time.sleep(10)

@app.cli.command("set-campaign-banner")
@click.argument("key", type=click.STRING)
@click.argument("filename", type=click.STRING)
@with_appcontext
def set_campaign_banner(key, filename):
    from utils import store_image

    campaign = db.session.query(m.Campaign).filter_by(key=key).one_or_none()
    if not campaign:
        app.logger.error("Campaign not found.")
        return

    if not os.path.exists(filename):
        app.logger.error("File not found.")
        return

    with open(filename, "rb") as f:
        data = f.read()

    url, _ = store_image(get_s3(), f"campaign_{key}_banner", True, filename, data)
    if not url:
        app.logger.error("Error saving banner.")
        return

    campaign.banner_url = url
    db.session.commit()

def get_token_from_request():
    return request.headers.get('X-Access-Token')

def get_user_from_token(token):
    if not token:
        return None

    try:
        data = jwt.decode(token, app.config['SECRET_KEY'], algorithms=["HS256"])
    except Exception:
        return None

    if 'user_id' in data:
        q = m.User.id == data['user_id']
    else:
        # NB: we started storing the user id in the JWT token recently,
        # but we don't want to forcefully log out all users that have old tokens,
        # so keep this code around for now, but we should get rid of it later!
        if 'user_key' in data:
            q = m.User.lnauth_key == data['user_key']
        elif 'user_lnauth_key' in data:
            q = m.User.lnauth_key == data['user_lnauth_key']
        elif 'user_nostr_public_key' in data:
            q = m.User.nostr_public_key == data['user_nostr_public_key']
        else:
            return None

    return m.User.query.filter(q).first()

def user_required(f):
    @wraps(f)
    def decorator(*args, **kwargs):
        token = get_token_from_request()
        if not token:
            return jsonify({'success': False, 'message': "Missing token."}), 401
        user = get_user_from_token(token)
        if not user:
            return jsonify({'success': False, 'message': "Invalid token."}), 401
        return f(user, *args, **kwargs)
    return decorator

class MockBTCClient:
    def get_funding_txs(self, addr):
        sale = db.session.query(m.Sale).filter(m.Sale.address == addr).first()
        if sale:
            confirmed = sale.txid is not None
            block_time = datetime.utcnow() - timedelta(seconds=5) if confirmed else None
            return [{'txid': 'MOCK_TXID', 'value': sale.amount + sale.shipping_domestic + int(sale.shipping_domestic / 100), 'confirmed': confirmed, 'block_time': block_time}]
        else:
            return []

class MempoolSpaceError(Exception):
    def __str__(self):
        return "Error reading from mempool API!"

class MempoolSpaceBTCClient:
    def get_funding_txs(self, addr):
        if addr.startswith("OLD_"): # not a real address, but a placeholder we used for sales from before we started accepting on-chain payments
            return []

        try:
            response_json = requests.get(f"https://mempool.space/{'testnet/' if addr.startswith('t') else ''}api/address/{addr}/txs").json()
        except JSONDecodeError as e:
            raise MempoolSpaceError() from e

        txs = []
        for tx in response_json:
            vout_for_addr = [vo for vo in tx['vout'] if vo.get('scriptpubkey_address') == addr]
            if len(vout_for_addr) > 1:
                app.logger.warning("Multiple outputs for same address? Strange...")
            value = sum(vo['value'] for vo in vout_for_addr)
            txs.append({
                'txid': tx['txid'],
                'value': value,
                'confirmed': tx['status']['confirmed'],
                'block_time': datetime.fromtimestamp(tx['status']['block_time']) if tx['status']['confirmed'] else None,
            })

        return txs

def get_btc_client():
    if app.config['MOCK_BTC']:
        return MockBTCClient()
    else:
        return MempoolSpaceBTCClient()

class Birdwatcher:
    def __init__(self, base_url):
        self.base_url = base_url

    def add_relay(self, relay_url):
        response = requests.post(f"{self.base_url}/relays", json={'url': relay_url})
        if response.status_code == 200:
            app.logger.info(f"Successfully POSTed relay {relay_url} to birdwatcher!")
            return True
        else:
            app.logger.error(f"Error POSTing relay {relay_url} to birdwatcher!")
            return False

    def post_event(self, event):
        event_json = json.loads(event.to_message())[1] # ugly as hell. maybe we should just completely get rid of this python-nostr library, it's been a pain in the ass!
        response = requests.post(f"{self.base_url}/events", json=event_json)
        if response.status_code == 200:
            app.logger.info(f"Successfully POSTed event {event.id} to birdwatcher: {event_json=}!")
            return True
        else:
            app.logger.error(f"Error POSTing event {event.id} to birdwatcher!")
            return False

    def send_dm(self, sender_private_key, recipient_public_key, body):
        try:
            dm = EncryptedDirectMessage(recipient_pubkey=recipient_public_key, cleartext_content=body)
            sender_private_key.sign_event(dm)
            if self.post_event(dm):
                return dm.id
        except:
            app.logger.exception(f"Error sending DM for {recipient_public_key} via birdwatcher!")

    def publish_stall(self, merchant):
        STALL_EVENT_KIND = 31017 if app.config['ENV'] == 'staging' else 30017
        stall_json = merchant.to_nostr_stall()
        try:
            event = Event(kind=STALL_EVENT_KIND, content=json.dumps(stall_json), tags=[['d', stall_json['id']]])
            merchant.parse_merchant_private_key().sign_event(event)
            if self.post_event(event):
                return event.id
        except:
            app.logger.exception(f"Error publishing stall for merchant {merchant.merchant_public_key} via birdwatcher!")

    def delete_stall(self, merchant):
        try:
            event = Event(kind=5, content=f"Stall \"{merchant.stall_name}\" deleted!", tags=[['e', merchant.stall_nostr_event_id]])
            merchant.parse_merchant_private_key().sign_event(event)
            if self.post_event(event):
                return event.id
        except:
            app.logger.exception(f"Error deleting stall for merchant {merchant.merchant_public_key} via birdwatcher!")

    def publish_product(self, entity, extra_media=None):
        product_json = entity.to_nostr(extra_media)
        try:
            event = Event(kind=entity.nostr_event_kind, content=json.dumps(product_json), tags=[['d', product_json['id']]])
            entity.item.seller.parse_merchant_private_key().sign_event(event)
            if self.post_event(event):
                return event.id
        except:
            app.logger.exception(f"Error publishing product for merchant {entity.item.seller.merchant_public_key} via birdwatcher!")

    def delete_product(self, entity):
        try:
            event = Event(kind=5, content=f"Item \"{entity.item.title}\" deleted!", tags=[['e', entity.nostr_event_id]])
            entity.item.seller.parse_merchant_private_key().sign_event(event)
            if self.post_event(event):
                return event.id
        except:
            app.logger.exception(f"Error deleting product for merchant {entity.item.seller.merchant_public_key} via birdwatcher!")

    def publish_bid_status(self, auction, bid_event_id, status, message=None, duration_extended=0, donation_stall_ids=None, extra_tags=None):
        BID_STATUS_EVENT_KIND = 2022 if app.config['ENV'] == 'staging' else 1022
        try:
            if extra_tags is None:
                extra_tags = []
            content_json = {'status': status}
            if message is not None:
                content_json['message'] = message
            if duration_extended != 0:
                content_json['duration_extended'] = duration_extended
            if donation_stall_ids is not None:
                content_json['donation_stall_ids'] = donation_stall_ids
            event = Event(kind=BID_STATUS_EVENT_KIND, content=json.dumps(content_json), tags=([['e', auction.nostr_event_id], ['e', bid_event_id]] + extra_tags))
            auction.item.seller.parse_merchant_private_key().sign_event(event)
            if self.post_event(event):
                return event.id
        except:
            app.logger.exception(f"Error publishing bid status for bid {bid_event_id} via birdwatcher!")

def get_birdwatcher():
    return Birdwatcher(app.config['BIRDWATCHER_BASE_URL'])

class MockS3:
    def get_url_prefix(self):
        return app.config['API_BASE_URL_EXTERNAL'] + "/mock-s3-files/"

    def get_filename_prefix(self):
        return ""

    def upload(self, data, filename):
        filename_with_prefix = self.get_filename_prefix() + filename
        app.logger.info(f"Upload {filename_with_prefix} to MockS3!")
        with open(f"/tmp/{filename_with_prefix}", "wb") as f:
            # basically store the content under /tmp to be used by the /mock-s3-files/ route later
            f.write(data)

class S3:
    def __init__(self, endpoint_url, key_id, application_key):
        self.s3 = boto3.resource(service_name='s3', endpoint_url=endpoint_url, aws_access_key_id=key_id, aws_secret_access_key=application_key, config=Config(signature_version='s3v4'))

    def get_url_prefix(self):
        return app.config['S3_URL_PREFIX']

    def get_filename_prefix(self):
        return app.config['S3_FILENAME_PREFIX']

    def upload(self, data, filename):
        self.s3.Bucket(app.config['S3_BUCKET']).upload_fileobj(io.BytesIO(data), self.get_filename_prefix() + filename)

def get_s3():
    if app.config['MOCK_S3']:
        return MockS3()
    else:
        with open(app.config['S3_SECRETS']) as f:
            s3_secrets = json.load(f)
        return S3(app.config['S3_ENDPOINT_URL'], s3_secrets['KEY_ID'], s3_secrets['APPLICATION_KEY'])

class Mail:
    def send(self, to, subject, body):
        msg = Message(subject, recipients=[to])
        msg.body = body
        mail.send(msg)

class MockMail:
    def send(self, to, subject, body):
        app.logger.info(f"Mail: {to=} {subject=} {body=}")

def get_mail():
    if app.config['MOCK_MAIL']:
        return MockMail()
    else:
        return Mail()

if __name__ == '__main__':
    import lnurl
    try:
        lnurl.encode(app.config['API_BASE_URL'])
    except lnurl.exceptions.InvalidUrl:
        # HACK: allow URLs with http:// and no TLD in development mode (http://localhost)
        from pydantic import AnyHttpUrl
        class ClearnetUrl(AnyHttpUrl):
            pass
        app.logger.warning("Patching lnurl.types.ClearnetUrl!")
        lnurl.types.ClearnetUrl = ClearnetUrl
        lnurl.encode(app.config['API_BASE_URL']) # try parsing again to check that the patch worked

    @app.route("/mock-s3-files/<string:filename>", methods=['GET'])
    def mock_s3(filename):
        app.logger.info(f"Fetch {filename} from MockS3!")
        with open(f"/tmp/{filename}", "rb") as f:
            data = f.read()
            return send_file(io.BytesIO(data), mimetype=magic.from_buffer(data, mime=True))

    app.run(host='0.0.0.0', port=5000, debug=True)
else:
    gunicorn_logger = logging.getLogger('gunicorn.error')
    app.logger.handlers = gunicorn_logger.handlers
    app.logger.setLevel(gunicorn_logger.level)


@app.cli.command("lnauth")
@click.argument("lnkey", type=click.STRING)
@with_appcontext
def store_lnauth_key(lnkey):
    """
    For dev env - simplifies passing by the ln-auth system
    Creates a ln auth entry with api key
    :param lnkey: str
    """
    click.echo(f"Setting latest LnAuth key: {lnkey}")
    ln = m.LnAuth.query.order_by(desc(m.LnAuth.created_at)).first()
    ln.key = lnkey
    db.session.commit()

@app.cli.command("lnverify")
@click.argument("lnkey", type=click.STRING)
@with_appcontext
def verify_lnauth_key(lnkey):
    """
    For dev env - simplifies verifying a "fake" lightning wallet.
    """
    click.echo(f"Setting latest key: {lnkey}")
    u = m.User.query.filter(m.User.new_lnauth_key_k1_generated_at != None).order_by(desc(m.User.new_lnauth_key_k1_generated_at)).first()
    u.lnauth_key = u.new_lnauth_key = lnkey
    db.session.commit()
