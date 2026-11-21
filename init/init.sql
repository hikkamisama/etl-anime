CREATE SCHEMA IF NOT EXISTS anime;

CREATE TABLE IF NOT EXISTS anime.images (
    image_id BIGINT PRIMARY KEY CHECK (image_id > 0),
    image_url TEXT NOT NULL CHECK (image_url ~ '^https?://'),
    rating TEXT NOT NULL CHECK (rating = 'safe'),
    artist_name TEXT,
    source_url TEXT,
    tags TEXT[] NOT NULL,
    tag_count INTEGER GENERATED ALWAYS AS (cardinality(tags)) STORED,
    dominant_color_hex CHAR(7),
    color_palette JSONB NOT NULL,
    extracted_at TIMESTAMPTZ NOT NULL,
    loaded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (dominant_color_hex IS NULL OR dominant_color_hex ~ '^#[0-9a-f]{6}$')
);
