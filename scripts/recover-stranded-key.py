#!/usr/bin/env python3
"""Re-encrypt records stranded under a wiped key — ALL encrypted columns.

The store is SPLIT: most records decrypt with the restored (original) key, and
those written after the key overwrite decrypt only with that overwritten key
(preserved as .crypto.key.pre-rotate). Both keys are held, so this is lossless:
each stranded row is decrypted with the key that works for it and re-encrypted
under the restored key.

THE `records` TABLE HAS THREE record-scoped encrypted columns
(store/_store.py _from_row):

    literal_surface               <- the memory text
    provenance_json               <- provenance list
    profile_modulation_gain_json  <- profile gain map

A key wipe strands the SAME row in every one of them. Migrating only
literal_surface leaves `InvalidTag` on provenance_json, which surfaces later as
`store.all_records()` failing inside the sleep pipeline (step USER_MODEL_UPDATE)
and backing the cycle off 600s. Hence: every column, one pass.

AAD is record-scoped (`_ad(record_id)` == `_aad_for_id(rid)`) and identical for
every column of a row — the record id is the context, not the column.

Safety:
  - census first; ABORTS with no writes if any row is unrecoverable in any column;
  - writes only values it successfully decrypted first (never a placeholder);
  - commits in one transaction, then re-verifies every value end to end;
  - idempotent: a second run finds nothing to migrate.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

STORE = Path(os.path.expanduser("~/.iai-mcp"))
NEWKEY_FILE = STORE / ".crypto.key.pre-rotate"   # the overwritten key

os.environ["IAI_MCP_STORE"] = str(STORE)
os.environ.pop("IAI_MCP_CRYPTO_PASSPHRASE", None)

from iai_mcp.crypto import CryptoKey, decrypt_field, encrypt_field, is_encrypted  # noqa: E402
from iai_mcp.hippo import HippoDB  # noqa: E402
from iai_mcp.hippo._recall import _aad_for_id  # noqa: E402

#: Every record-scoped encrypted column on `records`, per store/_store.py.
TABLES_FIELDS = [
    ("records", "literal_surface"),
    ("records", "provenance_json"),
    ("records", "profile_modulation_gain_json"),
]


def main() -> int:
    restored = CryptoKey(user_id="default", store_root=STORE).get_or_create()
    stranded_key = NEWKEY_FILE.read_bytes()
    if len(stranded_key) != 32:
        print(f"FATAL: {NEWKEY_FILE} is not 32 bytes", file=sys.stderr)
        return 2

    db = HippoDB(str(STORE))
    try:
        conn = db._conn
        total = ok = stranded = unrecoverable = 0
        plans: list[tuple[str, str, str, str]] = []   # table, id, new_ct, note

        for table, field in TABLES_FIELDS:
            rows = conn.execute(f"SELECT id, {field} FROM {table}").fetchall()
            for rid, blob in rows:
                if blob is None:
                    continue
                total += 1
                s = blob if isinstance(blob, str) else blob.decode("utf-8", "replace")
                if not is_encrypted(s):
                    continue
                aad = _aad_for_id(rid)
                try:
                    decrypt_field(s, restored, aad)
                    ok += 1
                    continue                      # already fine
                except Exception:
                    pass
                # stranded: must decrypt with the overwritten key
                try:
                    pt = decrypt_field(s, stranded_key, aad)
                except Exception as exc:
                    unrecoverable += 1
                    print(f"  UNRECOVERABLE {table}.{field}/{rid}: {type(exc).__name__}")
                    continue
                stranded += 1
                # NOTE: do NOT reject empty/whitespace plaintext. Provenance and
                # the profile gain map legitimately serialise as "[]" / "{}", and
                # a whitespace-only literal is a legal memory. Emptiness is not
                # evidence of a bad decrypt here — a bad decrypt raises, above.
                plans.append((table, field, rid, encrypt_field(pt, restored, aad)))

        print(f"  census: values={total} readable_with_restored={ok} "
              f"stranded={stranded} unrecoverable={unrecoverable}")
        if not plans:
            print("  nothing to migrate — every encrypted column is consistent")
            return 0
        if unrecoverable:
            print(f"  ABORTING: {unrecoverable} unrecoverable value(s); "
                  "refusing a partial migration without review", file=sys.stderr)
            return 3

        for table, field, rid, new_ct in plans:
            conn.execute(f"UPDATE {table} SET {field}=? WHERE id=?", (new_ct, rid))
        conn.commit()
        print(f"  committed {len(plans)} re-encrypted value(s)")

        # End-to-end re-verify with the restored key only.
        good = bad = 0
        for table, field in TABLES_FIELDS:
            for rid, blob in conn.execute(f"SELECT id, {field} FROM {table}").fetchall():
                if blob is None:
                    continue
                s = blob if isinstance(blob, str) else blob.decode("utf-8", "replace")
                if not is_encrypted(s):
                    continue
                try:
                    decrypt_field(s, restored, _aad_for_id(rid))
                    good += 1
                except Exception:
                    bad += 1
        print(f"  POST-VERIFY with restored key only: readable={good} unreadable={bad}")
        return 0 if bad == 0 else 4
    finally:
        try:
            db.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
