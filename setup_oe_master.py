"""
TEST SCAFFOLDING - gitignored, not production code.

Creates oe_master (PLAN.md Appendix A.1) on the REAL Postgres this app is
already connected to, and populates it with a small catalog using REAL
Bedrock Titan embeddings - not fake vectors. Connects directly via
django.db.connection (the same connection the app itself uses), no SSM
needed now that there's a direct TCP path to the EC2 Postgres.

Deliberately includes:
  - products matching the fixture mailbox's real order text, so a real
    end-to-end run can actually match something
  - the SAME product at two different pack sizes (30P vs 90P) under
    DIFFERENT oe_codes, to genuinely exercise the "pack size is a
    required exact match" rule (PLAN.md Appendix A.1) - a wrong-pack
    match here would be a real, visible bug, not a theoretical one
  - one intentionally ambiguous pair (near-identical text, different
    variant) so a real run also exercises the LLM rerank path
    (services.rerank_oe_candidates), not just the fast vector-only path

    python scripts/setup_oe_master.py
"""

import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "opticbot.settings")

import django  # noqa: E402
django.setup()

from django.db import connection  # noqa: E402
from optic_bot import services  # noqa: E402

DDL = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS oe_master (
    id               BIGSERIAL PRIMARY KEY,
    product_type     VARCHAR(10),
    oe_code          VARCHAR(20)  NOT NULL,
    uom              VARCHAR(10),
    fam_code         VARCHAR(10),
    base_curve       NUMERIC(4,2),
    brand            VARCHAR(20),
    brand_name       VARCHAR(255),
    variant_name     VARCHAR(255),
    type             VARCHAR(30),
    embedding_text   TEXT NOT NULL,
    embedding        vector(1024) NOT NULL,
    is_active        BOOLEAN NOT NULL DEFAULT TRUE,
    embedding_model  VARCHAR(100),
    created_at       TIMESTAMPTZ DEFAULT now(),
    updated_at       TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS oe_master_active_idx ON oe_master (is_active);
"""

# NOTE: no HNSW index here on purpose - see build() below, it's created
# AFTER loading rows (cheaper to build once than maintain during inserts),
# same guidance as PLAN.md Appendix A.1.

# (product_type, oe_code, uom, fam_code, base_curve, brand, brand_name, variant_name, type)
CATALOG = [
    # --- matches msg-002 fixture: "1-Day Acuvue Define - Accent Style" / "Natural Shine" ---
    ("00", "1FA", "30P", "GE", 8.5, "1DL", "1-DAY DEFINE WITH LACREON", "ACCENT", "SPHERICAL"),
    ("05", "1FA", "1P",  "GL", 8.5, "1DL", "1-DAY DEFINE WITH LACREON", "ACCENT", "SPHERICAL"),
    ("00", "1FA", "10P", "GT", 8.5, "1DL", "1-DAY DEFINE WITH LACREON", "ACCENT", "SPHERICAL"),
    ("00", "SFB", "30P", "HC", 8.5, "1DL", "1-DAY DEFINE WITH LACREON", "FRESH BLUE",    "SPHERICAL"),
    ("05", "SFB", "1P",  "HG", 8.5, "1DL", "1-DAY DEFINE WITH LACREON", "FRESH BLUE",    "SPHERICAL"),
    ("00", "SFZ", "30P", "HC", 8.5, "1DL", "1-DAY DEFINE WITH LACREON", "FRESH GRAYZEL", "SPHERICAL"),

    # --- matches msg-001 fixture: "Daily 1 Day Oasys 90pk" (Lighthouse PDF) ---
    ("00", "OM3", "90P", "OA", 8.5, "OAS", "OASYS MAX 1-DAY", "STANDARD", "SPHERICAL"),
    # SAME product, DIFFERENT pack - a wrong-pack match here is a real bug
    ("00", "OM1", "30P", "OA", 8.5, "OAS", "OASYS MAX 1-DAY", "STANDARD", "SPHERICAL"),

    # --- matches msg-003 fixture: "1-Day Acuvue Oasys for Astigmatism (30)", toric ---
    ("00", "AST", "30P", "OT", 8.5, "OAS", "OASYS 1-DAY", "ASTIGMATISM", "TORIC"),

    # --- matches msg-004/msg-005 fixtures: "Acuvue Oasys Dailies", trial ---
    ("00", "DLY", "90P", "OD", 8.6, "OAS", "OASYS DAILIES", "STANDARD", "SPHERICAL"),
    ("05", "TRO", "5P",  "OT", 8.6, "OAS", "OASYS 1-DAY", "ASTIGMATISM TRIAL", "TORIC"),

    # --- intentionally ambiguous pair: near-identical text, must exercise the LLM rerank ---
    ("00", "FGA", "30P", "HC", 8.5, "1DL", "1-DAY DEFINE WITH LACREON", "FRESH GRAYZEL ACCENT", "SPHERICAL"),
]


def build():
    with connection.cursor() as cur:
        cur.execute(DDL)
    print(f"oe_master table ready (pgvector extension confirmed).")

    with connection.cursor() as cur:
        cur.execute("SELECT count(*) FROM oe_master")
        existing = cur.fetchone()[0]
    if existing:
        print(f"oe_master already has {existing} row(s) - clearing before reseed.")
        with connection.cursor() as cur:
            cur.execute("TRUNCATE oe_master RESTART IDENTITY")

    print(f"Embedding {len(CATALOG)} catalog rows via real Bedrock "
          f"({services.settings.OE_EMBEDDING_MODEL_ID})...")

    for i, row in enumerate(CATALOG, 1):
        (product_type, oe_code, uom, fam_code, base_curve,
         brand, brand_name, variant_name, lens_type) = row

        label = services.build_product_label(brand_name, variant_name, lens_type)
        embedding = services.get_query_embedding(label)
        vector_literal = "[" + ",".join(f"{v:.8f}" for v in embedding) + "]"

        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO oe_master
                    (product_type, oe_code, uom, fam_code, base_curve, brand,
                     brand_name, variant_name, type, embedding_text, embedding,
                     is_active, embedding_model)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::vector,TRUE,%s)
                """,
                [product_type, oe_code, uom, fam_code, base_curve, brand,
                 brand_name, variant_name, lens_type, label, vector_literal,
                 services.settings.OE_EMBEDDING_MODEL_ID],
            )
        print(f"  [{i}/{len(CATALOG)}] {oe_code:<5} {uom:<4} {label}")

    with connection.cursor() as cur:
        cur.execute(
            "CREATE INDEX IF NOT EXISTS oe_master_embedding_idx ON oe_master "
            "USING hnsw (embedding vector_cosine_ops)"
        )
    print("HNSW index built.")

    with connection.cursor() as cur:
        cur.execute("SELECT count(*) FROM oe_master WHERE is_active")
        print(f"\noe_master ready: {cur.fetchone()[0]} active row(s).")
    print(services.oe_master_status())


if __name__ == "__main__":
    build()
