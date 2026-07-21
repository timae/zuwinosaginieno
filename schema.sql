-- Vivino wine data — Postgres schema
-- Stores wine descriptive data only (name / facts / categories).
-- No rating or price columns by design.

CREATE TABLE IF NOT EXISTS wines (
    vintage_id              BIGINT PRIMARY KEY,
    wine_id                 BIGINT NOT NULL,
    name                    TEXT,
    vintage_name            TEXT,
    vintage_year            TEXT,
    seo_name                TEXT,
    vivino_url              TEXT,

    -- categories
    wine_type_id            INTEGER,
    wine_type               TEXT,
    is_natural              BOOLEAN,

    -- style / facts
    style_id                BIGINT,
    style_name              TEXT,
    style_varietal_name     TEXT,
    style_description       TEXT,
    style_blurb             TEXT,
    style_body              INTEGER,
    style_body_description  TEXT,
    style_acidity           INTEGER,
    style_acidity_description TEXT,

    -- producer / origin
    winery_id               BIGINT,
    winery_name             TEXT,
    region_id               BIGINT,
    region_name             TEXT,
    country_code            TEXT,
    country_name            TEXT,

    -- nested descriptive facts kept as jsonb for flexibility
    grapes                  JSONB DEFAULT '[]'::jsonb,
    foods                   JSONB DEFAULT '[]'::jsonb,
    flavors                 JSONB DEFAULT '[]'::jsonb,

    ingested_at             TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_wines_wine_id      ON wines (wine_id);
CREATE INDEX IF NOT EXISTS idx_wines_type         ON wines (wine_type_id);
CREATE INDEX IF NOT EXISTS idx_wines_country      ON wines (country_code);
CREATE INDEX IF NOT EXISTS idx_wines_style        ON wines (style_id);
CREATE INDEX IF NOT EXISTS idx_wines_grapes_gin   ON wines USING gin (grapes);
CREATE INDEX IF NOT EXISTS idx_wines_flavors_gin  ON wines USING gin (flavors);

-- Optional normalized view of grapes for querying wine <-> grape relations:
--   SELECT w.name, g->>'name' AS grape
--   FROM wines w, jsonb_array_elements(w.grapes) g;
