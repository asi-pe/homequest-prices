-- HomeQuest — price comparison schema.
-- Run this ONCE in the Supabase SQL editor (Dashboard → SQL Editor → New query → paste → Run).
-- Safe to re-run (uses IF NOT EXISTS / CREATE OR REPLACE).

-- ============================================================
-- 1) prices table — one row per (barcode, chain)
-- ============================================================
create table if not exists public.prices (
    barcode      text  not null,
    chain        text  not null,   -- enum name, e.g. RAMI_LEVY
    chain_label  text  not null,   -- Hebrew label, e.g. רמי לוי
    name         text,
    price        numeric not null,
    updated_at   timestamptz not null default now(),
    primary key (barcode, chain)
);

-- Fast lookup by barcode (the comparison queries filter on a set of barcodes).
create index if not exists prices_barcode_idx on public.prices (barcode);
-- Trigram index for fuzzy name search (when the user typed text, not a barcode).
create extension if not exists pg_trgm;
create index if not exists prices_name_trgm_idx on public.prices using gin (name gin_trgm_ops);

-- ============================================================
-- 2) Row Level Security
--    The app reads with the PUBLISHABLE key → allow read-only to everyone.
--    The Python service writes with the SECRET key → bypasses RLS automatically.
-- ============================================================
alter table public.prices enable row level security;

drop policy if exists "prices_public_read" on public.prices;
create policy "prices_public_read"
    on public.prices for select
    using (true);

-- ============================================================
-- 3) compare_basket(items) — the app calls this.
--    Input: a JSON array of { barcode?, name?, quantity? }.
--    Output: one row per chain with total basket price + how many items it had,
--            ordered cheapest first. Items matched by exact barcode when given,
--            otherwise by best fuzzy name match within each chain.
-- ============================================================
create or replace function public.compare_basket(items jsonb)
returns table (
    chain          text,
    chain_label    text,
    total          numeric,
    matched        int,
    requested      int,
    missing_items  text[]
)
language sql
stable
as $$
with req as (
    select
        coalesce((i->>'quantity')::numeric, 1) as qty,
        nullif(i->>'barcode', '')              as barcode,
        nullif(i->>'name', '')                 as name
    from jsonb_array_elements(items) as i
),
-- Break each free-text item into meaningful search words:
--   • strip quantities/units (500, גרם, ק"ג, ליטר, מ"ל, יח', אריזה...) so
--     "בשר טחון 500 גרם" searches only for בשר + טחון.
--   • keep words of length >= 2.
-- We match a product when ALL of these words appear in its name (any order),
-- so "בשר טחון" also catches "בשר בקר טחון", "בשר טחון טרי", etc.
req_words as (
    select
        r.qty, r.barcode, r.name as requested_name,
        array_remove(
            array_agg(w) filter (
                where length(w) >= 2
                  and w !~ '^[0-9]'                                   -- drop pure numbers / "500"
                  and w not in ('גרם','גר','ג','קג','קילו','ליטר','מל',
                                'יח','יחי','יחידה','אריזה','חבילה','של',
                                'kg','gr','g','ml','l')
            ),
            null
        ) as words
    from req r
    cross join lateral regexp_split_to_table(
        regexp_replace(lower(coalesce(r.name, '')), '[^a-zא-ת0-9 ]', ' ', 'g'),
        '\s+'
    ) as w
    group by r.qty, r.barcode, r.name
),
-- For each requested line, find its price in every chain.
matched_lines as (
    select c.chain, c.chain_label, r.qty, r.requested_name, p.price
    from req_words r
    -- all chains we have prices for
    cross join (select distinct chain, chain_label from public.prices) c
    -- best matching product for this line within this chain
    left join lateral (
        select pr.price, pr.name as pr_name
        from public.prices pr
        where pr.chain = c.chain
          and (
                -- exact barcode wins when the user picked a specific product
                (r.barcode is not null and pr.barcode = r.barcode)
                -- otherwise: every search word must appear in the product name
             or (
                    r.barcode is null
                and r.words is not null
                and array_length(r.words, 1) >= 1
                and not exists (
                    select 1 from unnest(r.words) as kw
                    where pr.name not ilike '%' || kw || '%'
                )
                )
          )
        order by
            -- 1) exact barcode match first
            (case when r.barcode is not null and pr.barcode = r.barcode then 0 else 1 end),
            -- 2) prefer a product whose name STARTS with the first search word
            --    (so "בשר טחון..." beats "תבלין לבשר טחון")
            (case when r.words is not null
                   and pr.name ilike r.words[1] || '%' then 0 else 1 end),
            -- 3) then the shortest (closest) name among the remaining
            length(coalesce(pr.name, ''))
        limit 1
    ) p on true
)
select
    chain,
    chain_label,
    round(sum(coalesce(price, 0) * qty), 2)      as total,
    count(price)::int                            as matched,
    count(*)::int                                as requested,
    array_agg(requested_name) filter (where price is null)  as missing_items
from matched_lines
group by chain, chain_label
-- only show chains that matched at least one item
having count(price) > 0
order by total asc;
$$;

-- Allow the app (anon / publishable key) to call the function.
grant execute on function public.compare_basket(jsonb) to anon, authenticated;
