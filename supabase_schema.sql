-- Run this once in the Supabase SQL editor (Project → SQL Editor → New query)
-- to create the tables the bot needs.

create table if not exists subscribers (
    chat_id bigint primary key,
    chat_type text not null default 'unknown',
    subscribed_at timestamptz not null default now()
);

create table if not exists snapshots (
    target_url text primary key,
    content_hash text not null,
    raw_text text not null,
    sale_status text not null default 'UNKNOWN',
    updated_at timestamptz not null default now()
);

create table if not exists yeezy_products (
    product_id text primary key,
    title text not null default '',
    price text,
    url text not null default '',
    image_url text,
    first_seen_at timestamptz not null default now()
);
