# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""H6 frozen evaluation data and durable registry promotion."""

DDL = """
CREATE TABLE method_evaluations (
 method_id TEXT NOT NULL,
 method_version INTEGER NOT NULL,
 method_hash TEXT NOT NULL CHECK(length(method_hash)=64),
 set_hash TEXT NOT NULL CHECK(length(set_hash)=64),
 frozen_json TEXT NOT NULL CHECK(json_valid(frozen_json)),
 evidence_hash TEXT,
 evaluation_json TEXT CHECK(evaluation_json IS NULL OR json_valid(evaluation_json)),
 state TEXT NOT NULL CHECK(state IN ('FROZEN','EVALUATED','REJECTED','ADMITTED')),
 created_at REAL NOT NULL,
 updated_at REAL NOT NULL,
 PRIMARY KEY(method_id,method_version),
 FOREIGN KEY(method_id,method_version) REFERENCES method_contracts(method_id,method_version)
) STRICT;
"""
