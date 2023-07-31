import { writable, type Writable } from 'svelte/store';
import type { User } from "$lib/types/user";

export const user: Writable<User | null> = writable(null);

export type Placement = 'bottom-right' | 'bottom-left' | 'top-right' | 'top-left' | 'top-center' | 'bottom-center' | 'center-center';

export const Info: Writable<string | null | {message: string, duration: number, url: string, button: string, placement: Placement}> = writable(null);
export const Error: Writable<string | null> = writable(null);

export enum AuthBehavior {
    Login = "login",
}

export type AuthCallback = () => void;

export type AuthOptions = {
    default?: AuthBehavior,
    cb?: AuthCallback,
}

export const AuthRequired: Writable<AuthOptions | true | false> = writable(false);
