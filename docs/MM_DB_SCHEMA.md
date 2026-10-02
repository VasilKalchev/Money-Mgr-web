# Money Manager database — how to read it

A schema reference for the `.mmbak` files this app produces. There are no public
docs; everything here is reverse-engineered from real exports.

## Opening it

Despite the extension it is a plain SQLite3 database. No unpacking, no
decryption. Open it read-only so you cannot corrupt a real export.

```bash
sqlite3 "file:path/to/x.mmbak?mode=ro"
```

```python
con = sqlite3.connect("file:path/to/x.mmbak?mode=ro", uri=True)
```

`page_size = 4096`, UTF-8, `user_version = 19`. Run `PRAGMA table_info(<table>)`
before trusting any column name below. The app mixes `SCREAMING_CASE` and
`camelCase` with no pattern, and names shift between app versions.

## The five rules that matter most

**1. Join on the TEXT `uid`/`*Uid` columns, never the numeric `ID`/`AID`.** Every
table carries both. The numeric ids are leftovers that look like foreign keys and
will silently give you stale, wrong joins. `ASSETS.GROUP_ID` and
`ASSETS.CURRENCY_ID` disagree with what `groupUid` and `currencyUid` resolve to,
which is the proof.

**2. `ctgUid` is the only category pointer.** `categoryUid` and `ZDATA2` are
`NULL` on every row and mean nothing.

**3. Uids are not unique across trees, or even across tables.** `ZCATEGORY.TYPE`
0 is the income tree and 1 the expense tree, shown in separate pickers, and one
uid can appear in both, so every category query filters by `TYPE`. Category uids
also collide with account uids: a small numeric uid such as `'4'` can be both an
account in `ASSETS` and a category in `ZCATEGORY`. Look accounts up in `ASSETS`,
categories in `ZCATEGORY`, and never share a helper between them.

**4. Use `AMOUNT_ACCOUNT`, never `ZMONEY`.** `ZMONEY` is restated to whatever the
app's main currency was at export time, and the user can change the main
currency, so it is not comparable across exports. `AMOUNT_ACCOUNT` is stated
in the row's own account's currency, which is not always the main one.

**5. Filter `IS_DEL = 0`.** Soft-deleted transactions do occur, and their
`assetUid` can point at an account that no longer exists in `ASSETS`.

## Tables

The ones that hold data: `INOUTCOME`, `ZCATEGORY`, `ASSETS`, `ASSETGROUP`,
`CURRENCY`, `BUDGET`, `BUDGET_AMOUNT`, `ZETC`, `REPEATTRANSACTION`,
`FAVTRANSACTION`, `TAG`.

Usually empty and safe to ignore: `CODE_DATA`, `CODE_LINK`, `MEMO`,
`MESSAGEMACRO2`, `PHOTO`, `SMS_RAW_READ`, `TX_TAG`. `TAG` holds strings shipped
with the app, not user data. `ZETC` is app settings, one row per key
(`dataTypeKey`/`ZDATA`), not ledger data. `android_metadata` and
`sqlite_sequence` are SQLite's own.

### `INOUTCOME` — transactions

One row per transaction, or one per leg for transfers.

| Column | Meaning |
|---|---|
| `uid` | The real primary key. Use for dedup, joins, external ids. |
| `AID` | Legacy numeric id. Don't use. |
| `WDATE` | The transaction's date, TEXT `YYYY-MM-DD`. Date only, and the column to filter and group by. |
| `ZDATE` | The same moment with a time of day, epoch **milliseconds** as TEXT. This is where the transaction's clock time lives. See below. |
| `DO_TYPE` | Transaction kind, TEXT (`'1'`, not `1`). See below. |
| `ZCONTENT` | Free-text memo, the line the app shows under a transaction. Often empty. |
| `ZDATA` | A **second** free-text field, the app's longer note. Rarely used, but not dead. Don't overwrite it, don't rely on it. |
| `AMOUNT_ACCOUNT` | The amount, in **this row's own account's currency**. The number to trust. Always positive; direction comes from `DO_TYPE`. |
| `IN_ZMONEY` | The amount in the currency the transaction was actually entered in. |
| `ZMONEY` | Restated to the main currency at export time. A trap, see rule 4. |
| `assetUid` | The account this row belongs to (the source, for expenses and transfers) → `ASSETS.uid` |
| `toAssetUid` | Transfer destination. Meaningful only on `DO_TYPE` 3/4, `''` elsewhere. |
| `currencyUid` | The transaction's own currency → `CURRENCY.uid`. Compare against the account's currency to know whether conversion applied. |
| `ctgUid` | **The category join column** → `ZCATEGORY.uid`. Never `NULL`, see coverage below. |
| `txUidTrans` | Pairs the two legs of a transfer, so each value appears on exactly two rows; `''` on the rest. |
| `txUidFee` | Ties a transfer to its fee. The transfer's two legs and the fee's own expense row (`DO_TYPE` `'1'`) share one value, which is not the uid of any row. `''` or `NULL` on every row without a fee. |
| `IS_DEL` | Soft-delete flag. Always filter it. |
| `CARDDIVIDMONTH` | Credit-card instalment count. Almost always `'0'`. Ignore. |
| `MARK`, `syncVersion`, `isSynced` | Integer `0` on every row. Purpose unknown. |
| `UTIME` | Last-write timestamp, epoch ms. Bulk edits stamp their own value here. |

**Dead columns, `NULL` or empty on every row.** Do not build on any of them:
`ASSET_GROUP`, `ASSET_ID`, `ASSET_NIC`, `ASSET_NAME`, `CATEGORY_ID`,
`CATEGORY_NAME`, `CURRENCY_ID`, `FEE_ID`, `OPPOSITEAID`, `CARDDIVIDID`,
`CARD_DIVIDE_CID`, `CARD_TIME_STAMP_STR`, `CARD_DIVIDE_MONTH_STR`,
`categoryUid`, `ZDATA1`, `ZDATA2`, `SMS_RDATE`, `SMS_ORIGIN`,
`SMS_PARSE_CONTENT`, `SYNC_CHECK`, `TX_UID`, `syncTime`, `IMPORTANT`,
`cardDivideUid`, `lat`, `lng`, `gstd`, `wtime`, `paid`.

**A transaction's date and time are split across two columns.** The entry form
takes a date and a time, and the app displays both. `WDATE` stores the date, and
`ZDATE` stores the whole moment, so the time of day is the time part of `ZDATE`
read in local time. Neither is a write timestamp. The form prefills with the
current date and time, so a row entered without touching the pickers carries the
moment it was typed, but both are the user's to set and both can be backdated.

Three properties to build around:

- **The two columns agree, but only by convention.** Nothing in the schema ties
  them. Disagreements come from tools that bulk-write rows with `WDATE` set per
  row and `ZDATE` set to one constant instant, and from editing a saved row's
  date in the app, which can leave one row of a same-`ZDATE` cluster on the next
  day in `WDATE`. So write both columns together, and treat a mismatch as a repair job
  rather than as meaningful.
- **`ZDATE` is not unique and its seconds are not per-transaction.** Most rows
  share a timestamp with at least one other, because entering several purchases
  in one sitting stamps them all with the same second. Within a day that
  clustering is what recovers entry order; it is not an ordering between rows in
  the same cluster.
- **Read it as a number.** `CAST(ZDATE AS INTEGER)` before comparing, then
  convert with `'unixepoch', 'localtime'`.

`UTIME` is the closest thing to a real write timestamp, but bulk edits stamp
their own value over it, so it does not survive tooling.

**`DO_TYPE` values:**

| | |
|---|---|
| `1` | expense |
| `0` | income |
| `3` | transfer, primary leg. `assetUid` source → `toAssetUid` destination, fully self-describing |
| `4` | transfer, mirror of its `3` row (same `txUidTrans`). **Skip when listing transactions.** Its `assetUid` is the *receiving* account, so it is the row to read for that side's own-currency amount |
| `7` / `8` | balance adjustment, increase / decrease |

**Balance adjustments are how the app records a manual correction.** The user
enters one when the app's balance has drifted from the real account, so each is a
reconciliation against a real figure. They are stored as a **delta, not a target
balance**, which means adding or removing a transaction elsewhere does not cause
an adjustment to re-absorb the difference. Most accounts' opening balance is also
an adjustment. The app has no opening-balance field, and `ASSETS.AMOUNT` is empty
on every row.

They are also the only row type the app leaves out of its statistics, which makes
them the right shape for anything that moves a balance without being income or
spending, such as marking a securities holding to market.

**An account balance is therefore derived, not stored.** It is the sum of that
account's own rows: `0` and `7` add; `1`, `8` and `3` subtract; `4` adds, because
it carries the receiving side.

**`ctgUid` coverage.** These four cases partition every live row:

| `ctgUid` | What it is |
|---|---|
| a child category | the normal case |
| a **root** category | roots are used directly, so a report that assumes every `ctgUid` is a leaf drops these |
| `''` | transfer legs, by design |
| `'-4'` | the balance-adjustment sentinel. Matches nothing in `ZCATEGORY` |

LEFT JOIN and count the misses. A category lookup must **also** filter on the
row's tree, and the tree comes from `DO_TYPE`: income rows (`'0'`) resolve in
`TYPE = 0`, everything else in `TYPE = 1`. That is why a balance-adjustment row
can never safely carry a real category. An income-tree uid on a type-7 row
resolves to nothing today and to the *wrong* category the day two trees share
that uid (rule 3), so anything identifying a subset of adjustments has to key off
`ZCONTENT` instead. The app writes the bare word `Difference` there for an
adjustment entered in the app.

### `ZCATEGORY` — categories

Columns that matter: `uid`, `NAME`, `STATUS`, `pUid`, `TYPE`,
`ORDERSEQ`, `C_IS_DEL`. `PID` is the stale numeric twin of `pUid`.

A strict **two-level** tree. `STATUS = 0` is a root and `STATUS = 2` a child, and
a child's `pUid` is its parent's `uid`. A root's `pUid` is not meaningful, don't
chase it.

**`C_IS_DEL` tombstones a category.** The app sets it to `1` to hide a category
from the picker while keeping it so old transactions still resolve. The live
value is `''` or `NULL` and never `0`, so a reader needs
`IFNULL(NULLIF(C_IS_DEL, ''), 0) + 0 <> 1` rather than `= 0`.

**`ORDERSEQ` is the display order.** It should be contiguous and zero-based
within each (`TYPE`, `STATUS`, `pUid`) sibling group. The app itself does not
enforce that: it shows the tree in whatever order it finds, so a gap or
a duplicate is invisible until you open the app. Inserting a category means
renumbering its siblings, not appending `MAX + 1`. Assert contiguity after any
edit.

Uids can be mixed: numeric ones created by the app and UUIDs created by other
tools. The app only requires names to be unique within a parent and tree.
Categories with no rows are common, mostly roots that exist as headings.

### `ASSETS` — accounts

Columns that matter: `uid`, `NIC_NAME`, `currencyUid`, `ORDERSEQ`,
`groupUid`, `A_UTIME`. **`AMOUNT` is empty on every row**, so it is not the
balance.

`groupUid` → `ASSETGROUP`, whose `ACC_GROUP_NAME` is the heading the app files an
account under (`Cash`, `Loan`, ...). The group is a label, not a fact: an account in
`Loan` is not necessarily a loan.

`ASSETS.ORDERSEQ` is whatever the app wrote. Unlike `ZCATEGORY.ORDERSEQ` it is
not guaranteed contiguous or unique, so don't assert on it.

Account names can repeat, and in two different ways. Two accounts with the same
name and **different currencies** are usually one account the app converted plus
the old one left behind. Two with the same name and the **same currency** are
usually an account deleted in the app and remade later, with non-overlapping date
ranges. The distinction decides what a merge may do, see the currency section.

### `CURRENCY`

One row per currency in use. `uid` is the join target, `<main>_<ISO>` (`EUR_EUR`,
`EUR_USD`, ...), `ISO` the code, `IS_MAIN_CURRENCY` marks the main one, and
`DECIMAL_POINT` is the real precision: 2 for most currencies, **0 for JPY**.
Round to it.

`RATE` is units of the main currency per 1 unit of this currency and is **valid
only within one export**: the main currency is `1.0`. It is not a historical rate and not a way to reconstruct history.

### `BUDGET`

Joined by `CATEGORY_ID` **cast to TEXT** against `ZCATEGORY.uid`. A
`NULL` category means the overall budget. Anything that deletes or merges a
category must check this table or it leaves a dangling budget.

### `REPEATTRANSACTION`, `FAVTRANSACTION`

Recurring-transaction templates and saved favourites. They mirror
`INOUTCOME`'s uid columns (`assetUid`, `ctgUid`, `currencyUid`) and are not part
of the ledger. Renaming or deleting a category or account leaves them dangling
unless you follow through.

## Currency in a converted ledger

This app converts a currency **in place**. It does not create a second account
and it does not move a single transaction, so an export taken after a switch is
internally inconsistent by design and you have to read it deliberately.

- **`ASSETS.currencyUid` is rewritten** on the accounts in active use. Every row
  in `ASSETS` then carries the same `A_UTIME`, one bulk write over the table.
- **`AMOUNT_ACCOUNT` is restated** on every row of a converted account, at full
  float precision, at the rate the app used for the switch. For example, after
  a BGN account moves to EUR, a row entered as `IN_ZMONEY` `100.00` BGN reads
  `51.12918811...` in `AMOUNT_ACCOUNT`, exactly ÷ 1.95583, the official peg.
- **The transaction's own `currencyUid` is left alone.** After a switch, most
  older rows disagree with their account's currency. A row remembers what it was paid in;
  only the account-denominated amount moved.
- **Not every account is converted.** Accounts can stay in another currency,
  and the app converts a main-currency row *into* the account's currency for
  them.

So `AMOUNT_ACCOUNT` is always the account's currency, but that currency is not
always the main one. **Never sum `AMOUNT_ACCOUNT` across accounts without
checking `ASSETS.currencyUid`.** With, say, a yen account in the ledger this is
not a rounding-level mistake.

The same holds for merging accounts. A same-currency merge only repoints
`assetUid` and `toAssetUid`. A cross-currency merge has to restate
`AMOUNT_ACCOUNT` on every row it moves, and is not safe any other way.

Note also that a gradual shift in `currencyUid` over some months is a real-world
currency changeover (a dual-circulation period), not an app event.

## Ad-hoc queries

Sanity-check that you are looking at a full export:

```sql
SELECT DO_TYPE, COUNT(*) FROM INOUTCOME WHERE IS_DEL = 0 GROUP BY DO_TYPE;
SELECT COUNT(*), MIN(WDATE), MAX(WDATE) FROM INOUTCOME WHERE IS_DEL = 0;
```

Every account with its currency, transaction count and derived balance. This is
also how you spot duplicate names and dead accounts:

```sql
SELECT a.NIC_NAME, c.ISO, COUNT(i.uid) AS rows,
       ROUND(SUM(CASE i.DO_TYPE
           WHEN '0' THEN i.AMOUNT_ACCOUNT      -- income
           WHEN '7' THEN i.AMOUNT_ACCOUNT      -- adjustment up
           WHEN '4' THEN i.AMOUNT_ACCOUNT      -- transfer, receiving leg
           ELSE -i.AMOUNT_ACCOUNT END), 2) AS balance
FROM ASSETS a
LEFT JOIN CURRENCY c  ON c.uid = a.currencyUid
LEFT JOIN INOUTCOME i ON i.assetUid = a.uid AND i.IS_DEL = 0
GROUP BY a.uid ORDER BY rows DESC;
```

The category tree, one row per category, with the tree it belongs to. **The
`total` column lies if any account in scope is not the main currency**, so
multiply by `CURRENCY.RATE` the moment more than one currency is in play:

```sql
SELECT CASE c.TYPE WHEN 0 THEN 'income' ELSE 'expense' END AS tree,
       COALESCE(p.NAME || ' > ', '') || c.NAME AS path,
       COUNT(i.uid) AS rows,
       ROUND(IFNULL(SUM(i.AMOUNT_ACCOUNT), 0), 2) AS total
FROM ZCATEGORY c
LEFT JOIN ZCATEGORY p ON p.uid = c.pUid
LEFT JOIN INOUTCOME i ON i.ctgUid = c.uid AND i.IS_DEL = 0
GROUP BY c.uid ORDER BY tree DESC, path;
```

A transaction list with its date, time and category path. Note the LEFT JOINs,
which is what keeps the transfer legs and the `'-4'` sentinel rows visible
instead of silently dropped:

```sql
SELECT i.WDATE,
       time(CAST(i.ZDATE AS INTEGER) / 1000, 'unixepoch', 'localtime') AS tx_time,
       i.AMOUNT_ACCOUNT, cu.ISO, a.NIC_NAME,
       COALESCE(p.NAME || ' > ', '') || COALESCE(c.NAME, '(none)') AS category,
       i.ZCONTENT
FROM INOUTCOME i
LEFT JOIN ZCATEGORY c ON c.uid = i.ctgUid
                     AND c.TYPE = (CASE WHEN i.DO_TYPE = '0' THEN 0 ELSE 1 END)
LEFT JOIN ZCATEGORY p ON p.uid = c.pUid AND p.TYPE = c.TYPE
LEFT JOIN ASSETS a    ON a.uid = i.assetUid
LEFT JOIN CURRENCY cu ON cu.uid = i.currencyUid
WHERE i.IS_DEL = 0 AND i.DO_TYPE <> '4'      -- skip the mirror transfer leg
ORDER BY i.WDATE, CAST(i.ZDATE AS INTEGER);
```

Everything that has no description, by category:

```sql
SELECT COALESCE(p.NAME || ' > ', '') || c.NAME AS path, COUNT(*) AS blanks
FROM INOUTCOME i
JOIN ZCATEGORY c ON c.uid = i.ctgUid AND c.TYPE = 1
LEFT JOIN ZCATEGORY p ON p.uid = c.pUid AND p.TYPE = c.TYPE
WHERE i.IS_DEL = 0 AND i.DO_TYPE = '1' AND (i.ZCONTENT IS NULL OR i.ZCONTENT = '')
GROUP BY c.uid ORDER BY blanks DESC;
```
