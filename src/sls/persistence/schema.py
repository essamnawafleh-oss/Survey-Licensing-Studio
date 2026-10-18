"""Ordered schema migrations. Never edit an applied migration; append a new one."""
from __future__ import annotations

MIGRATIONS: list[tuple[int, str, str]] = [
    (
        1,
        "initial schema",
        """
CREATE TABLE users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL CHECK (role IN ('surveyor','reviewer','admin')),
    password_hash TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    username TEXT NOT NULL,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL DEFAULT '',
    case_id TEXT,
    details_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX ix_audit_case ON audit_log(case_id);

-- Configurable case form fields (authority forms are not hardcoded).
CREATE TABLE case_field_defs (
    key TEXT PRIMARY KEY,
    label_ar TEXT NOT NULL,
    label_en TEXT NOT NULL,
    field_type TEXT NOT NULL CHECK (field_type IN ('text','multiline','date','number','choice')),
    choices_json TEXT NOT NULL DEFAULT '[]',
    required INTEGER NOT NULL DEFAULT 0,
    sensitive INTEGER NOT NULL DEFAULT 0,
    sort_order INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE files (
    id TEXT PRIMARY KEY,
    case_id TEXT REFERENCES cases(id) ON DELETE RESTRICT,
    category TEXT NOT NULL CHECK (category IN (
        'drawing_source','drawing_revision','license_doc','photo','attachment','report','rule_workbook')),
    original_name TEXT NOT NULL,
    stored_path TEXT NOT NULL UNIQUE,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    imported_at TEXT NOT NULL,
    imported_by TEXT NOT NULL
);
CREATE INDEX ix_files_case ON files(case_id);

CREATE TABLE rule_sets (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE rule_set_versions (
    id TEXT PRIMARY KEY,
    rule_set_id TEXT NOT NULL REFERENCES rule_sets(id) ON DELETE RESTRICT,
    version INTEGER NOT NULL,
    workbook_file_id TEXT NOT NULL REFERENCES files(id),
    workbook_name TEXT NOT NULL,
    workbook_sha256 TEXT NOT NULL,
    sheet_name TEXT NOT NULL,
    header_row INTEGER NOT NULL,
    mapping_json TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    imported_by TEXT NOT NULL,
    verified INTEGER NOT NULL DEFAULT 0,
    verified_by TEXT NOT NULL DEFAULT '',
    verified_at TEXT NOT NULL DEFAULT '',
    UNIQUE (rule_set_id, version)
);

CREATE TABLE land_use_rules (
    id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES rule_set_versions(id) ON DELETE RESTRICT,
    code TEXT NOT NULL,
    name_ar TEXT NOT NULL DEFAULT '',
    name_en TEXT NOT NULL DEFAULT '',
    setbacks_json TEXT NOT NULL,          -- {"front": 4.0, "side": 3.0, "rear": 3.0, ...}
    max_coverage_pct REAL,
    max_floors INTEGER,
    max_far REAL,
    notes TEXT NOT NULL DEFAULT '',
    source_row INTEGER NOT NULL,
    UNIQUE (version_id, code)
);

CREATE TABLE cases (
    id TEXT PRIMARY KEY,
    case_number TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('draft','in_progress','in_review','closed','reopened')),
    parcel_identifier TEXT NOT NULL DEFAULT '',
    survey_date TEXT NOT NULL DEFAULT '',
    surveyor TEXT NOT NULL DEFAULT '',
    rule_version_id TEXT REFERENCES rule_set_versions(id),
    land_use_code TEXT NOT NULL DEFAULT '',
    units TEXT NOT NULL DEFAULT 'unknown',
    units_verified INTEGER NOT NULL DEFAULT 0,
    units_verified_by TEXT NOT NULL DEFAULT '',
    crs_id TEXT NOT NULL DEFAULT '',
    crs_kind TEXT NOT NULL DEFAULT 'unknown',
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE case_field_values (
    case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    field_key TEXT NOT NULL REFERENCES case_field_defs(key),
    value TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (case_id, field_key)
);

CREATE TABLE parcels (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    identifier TEXT NOT NULL DEFAULT '',
    boundary_entity_id TEXT NOT NULL DEFAULT '',
    boundary_points_json TEXT NOT NULL DEFAULT '[]',  -- verified boundary snapshot (drawing units)
    edge_types_json TEXT NOT NULL DEFAULT '[]',
    verified INTEGER NOT NULL DEFAULT 0,
    verified_by TEXT NOT NULL DEFAULT '',
    verified_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE drawings (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('survey','regulatory')),
    name TEXT NOT NULL,
    source_file_id TEXT REFERENCES files(id),
    import_report_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE drawing_revisions (
    id TEXT PRIMARY KEY,
    drawing_id TEXT NOT NULL REFERENCES drawings(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    model_file_id TEXT NOT NULL REFERENCES files(id),
    content_sha256 TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    UNIQUE (drawing_id, revision)
);

CREATE TABLE licenses (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    license_number TEXT NOT NULL,
    issue_date TEXT NOT NULL DEFAULT '',
    structure_description TEXT NOT NULL DEFAULT '',
    fee_amount REAL,
    fee_currency TEXT NOT NULL DEFAULT '',
    source_kind TEXT NOT NULL DEFAULT 'transcribed'
        CHECK (source_kind IN ('transcribed','ocr_reviewed','imported')),
    verification_status TEXT NOT NULL DEFAULT 'unverified'
        CHECK (verification_status IN ('unverified','verified','disputed')),
    reviewer_notes TEXT NOT NULL DEFAULT '',
    verified_by TEXT NOT NULL DEFAULT '',
    verified_at TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX ix_licenses_case ON licenses(case_id);

CREATE TABLE license_areas (
    id TEXT PRIMARY KEY,
    license_id TEXT NOT NULL REFERENCES licenses(id) ON DELETE CASCADE,
    floor_key TEXT NOT NULL,
    area_m2 REAL NOT NULL CHECK (area_m2 >= 0),
    note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE license_lengths (
    id TEXT PRIMARY KEY,
    license_id TEXT NOT NULL REFERENCES licenses(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('wall','fence')),
    length_m REAL NOT NULL CHECK (length_m >= 0),
    note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE license_violations (
    id TEXT PRIMARY KEY,
    license_id TEXT NOT NULL REFERENCES licenses(id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    area_m2 REAL
);

CREATE TABLE license_documents (
    id TEXT PRIMARY KEY,
    license_id TEXT NOT NULL REFERENCES licenses(id) ON DELETE CASCADE,
    file_id TEXT NOT NULL REFERENCES files(id),
    page_refs TEXT NOT NULL DEFAULT '',
    include_in_report INTEGER NOT NULL DEFAULT 1,
    page_selection TEXT NOT NULL DEFAULT '',   -- '' = all pages, else '1,3-4'
    note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE ocr_texts (
    id TEXT PRIMARY KEY,
    file_id TEXT NOT NULL REFERENCES files(id),
    engine TEXT NOT NULL,
    languages TEXT NOT NULL,
    text TEXT NOT NULL,
    reviewed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE photos (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    file_id TEXT NOT NULL REFERENCES files(id),
    caption TEXT NOT NULL DEFAULT '',
    taken_at TEXT NOT NULL DEFAULT '',
    include_in_report INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE attachments (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    file_id TEXT NOT NULL REFERENCES files(id),
    title TEXT NOT NULL DEFAULT '',
    include_in_report INTEGER NOT NULL DEFAULT 1,
    page_selection TEXT NOT NULL DEFAULT '',
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE calculations (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    engine_version TEXT NOT NULL,
    drawing_revision_id TEXT REFERENCES drawing_revisions(id),
    rule_version_id TEXT REFERENCES rule_set_versions(id),
    input_hash TEXT NOT NULL,
    result_hash TEXT NOT NULL,
    inputs_json TEXT NOT NULL,
    results_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ok','error')),
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    UNIQUE (case_id, revision)
);

CREATE TABLE calculation_licenses (
    calculation_id TEXT NOT NULL REFERENCES calculations(id) ON DELETE CASCADE,
    license_id TEXT NOT NULL REFERENCES licenses(id),
    license_revision INTEGER NOT NULL,
    PRIMARY KEY (calculation_id, license_id)
);

CREATE TABLE case_notes (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    section TEXT NOT NULL DEFAULT 'general',
    text TEXT NOT NULL,
    author TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE report_exports (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('draft','reviewed','approved')),
    calculation_id TEXT REFERENCES calculations(id),
    file_id TEXT NOT NULL REFERENCES files(id),
    manifest_json TEXT NOT NULL,
    options_json TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    generated_by TEXT NOT NULL,
    UNIQUE (case_id, revision)
);
""",
    ),
    (
        2,
        "rule raw values and per-case reviewer setback values",
        """
ALTER TABLE land_use_rules ADD COLUMN status TEXT NOT NULL DEFAULT 'numeric';
ALTER TABLE land_use_rules ADD COLUMN raw_json TEXT NOT NULL DEFAULT '{}';

-- Values entered by an authorised reviewer for one case when the rule table text is
-- conditional (e.g. "4م بعد 14م"). Valid only for the exact rule version and code.
CREATE TABLE case_setback_overrides (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    rule_version_id TEXT NOT NULL REFERENCES rule_set_versions(id),
    land_use_code TEXT NOT NULL,
    setbacks_json TEXT NOT NULL,
    note TEXT NOT NULL,
    entered_by TEXT NOT NULL,
    entered_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX ix_overrides_case ON case_setback_overrides(case_id);
""",
    ),
    (
        3,
        "cadastral parcel register (GIS parcel layers)",
        """
-- One row per imported parcel layer version. Source files are kept in the store under
-- shared/cadastre/; source_json lists their names, stored paths and SHA-256.
CREATE TABLE cadastre_layers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    version INTEGER NOT NULL,
    crs_code TEXT NOT NULL DEFAULT '',
    crs_wkt TEXT NOT NULL DEFAULT '',
    encoding TEXT NOT NULL,
    mapping_json TEXT NOT NULL,
    source_json TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    record_count INTEGER NOT NULL,
    imported_at TEXT NOT NULL,
    imported_by TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    UNIQUE (name, version)
);

CREATE TABLE cadastre_parcels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    layer_id TEXT NOT NULL REFERENCES cadastre_layers(id) ON DELETE CASCADE,
    fid INTEGER NOT NULL,
    dls_key TEXT NOT NULL DEFAULT '',
    village TEXT NOT NULL DEFAULT '',
    basin TEXT NOT NULL DEFAULT '',
    sector TEXT NOT NULL DEFAULT '',
    sheet TEXT NOT NULL DEFAULT '',
    parcel_no TEXT NOT NULL DEFAULT '',
    globalid TEXT NOT NULL DEFAULT '',
    attrs_json TEXT NOT NULL,
    wkb BLOB,
    area_m2 REAL,
    parts INTEGER NOT NULL DEFAULT 0,
    minx REAL, miny REAL, maxx REAL, maxy REAL,
    valid INTEGER NOT NULL,
    issue TEXT NOT NULL DEFAULT ''
);
CREATE INDEX ix_cad_key ON cadastre_parcels(layer_id, dls_key);
CREATE INDEX ix_cad_admin ON cadastre_parcels(layer_id, village, basin, sector, parcel_no);
CREATE INDEX ix_cad_bbox ON cadastre_parcels(layer_id, minx, maxx, miny, maxy);

INSERT OR IGNORE INTO case_field_defs(key,label_ar,label_en,field_type,sort_order)
    VALUES ('sheet_no','رقم اللوحة','Sheet number','text',9);
INSERT OR IGNORE INTO case_field_defs(key,label_ar,label_en,field_type,sort_order)
    VALUES ('dls_key','المفتاح الموحد (دائرة الأراضي)','DLS parcel key','text',10);
""",
    ),
    (
        4,
        "zoning register (land-use zone polygons, e.g. parcels x zoning intersect layer)",
        """
CREATE TABLE zoning_layers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    version INTEGER NOT NULL,
    crs_code TEXT NOT NULL DEFAULT '',
    encoding TEXT NOT NULL,
    mapping_json TEXT NOT NULL,
    source_json TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    record_count INTEGER NOT NULL,
    imported_at TEXT NOT NULL,
    imported_by TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    UNIQUE (name, version)
);

CREATE TABLE zoning_pieces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    layer_id TEXT NOT NULL REFERENCES zoning_layers(id) ON DELETE CASCADE,
    fid INTEGER NOT NULL,
    zone_id TEXT NOT NULL DEFAULT '',
    zone_name TEXT NOT NULL DEFAULT '',
    dls_key TEXT NOT NULL DEFAULT '',
    wkb BLOB,
    area_m2 REAL,
    minx REAL, miny REAL, maxx REAL, maxy REAL,
    valid INTEGER NOT NULL,
    issue TEXT NOT NULL DEFAULT ''
);
CREATE INDEX ix_zone_bbox ON zoning_pieces(layer_id, minx, maxx, miny, maxy);
CREATE INDEX ix_zone_key ON zoning_pieces(layer_id, dls_key);
""",
    ),
    (
        5,
        "setback construction approvals (reviewer-signed, fingerprinted offset constructions)",
        """
CREATE TABLE setback_approvals (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    method TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    fingerprint_parts_json TEXT NOT NULL,
    issue_keys_json TEXT NOT NULL,
    construction_json TEXT NOT NULL,
    rule_version_id TEXT,
    drawing_id TEXT,
    drawing_revision_id TEXT,
    note TEXT NOT NULL DEFAULT '',
    approved_by TEXT NOT NULL,
    approved_at TEXT NOT NULL
);
CREATE INDEX ix_setback_approvals_case ON setback_approvals(case_id, approved_at);
""",
    ),
]

# Default configurable case fields, inserted once by migration seeding (editable later).
DEFAULT_CASE_FIELDS: list[dict[str, object]] = [
    {"key": "owner_name", "label_ar": "اسم المالك / مقدم الطلب", "label_en": "Owner / applicant name",
     "field_type": "text", "sensitive": 1},
    {"key": "owner_contact", "label_ar": "وسيلة الاتصال", "label_en": "Contact",
     "field_type": "text", "sensitive": 1},
    {"key": "governorate", "label_ar": "المحافظة", "label_en": "Governorate", "field_type": "text"},
    {"key": "directorate", "label_ar": "المديرية / اللواء", "label_en": "Directorate", "field_type": "text"},
    {"key": "village", "label_ar": "القرية", "label_en": "Village", "field_type": "text"},
    {"key": "basin", "label_ar": "الحوض", "label_en": "Basin", "field_type": "text"},
    {"key": "neighbourhood", "label_ar": "الحي", "label_en": "Neighbourhood", "field_type": "text"},
    {"key": "plot_number", "label_ar": "رقم القطعة", "label_en": "Plot number", "field_type": "text"},
    {"key": "application_date", "label_ar": "تاريخ الطلب", "label_en": "Application date",
     "field_type": "date"},
]
