# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Migration 27: one live review pin per review *object*, not per blob.

A pin keeps bytes alive for one object of one review; reads are verified per
object.  Two objects with byte-identical content (a later leaf reading an earlier
leaf's output) must both pin in one review, sharing the blob.  Keying the live
guard on the blob hash rejected the second object on every recheck (2026-09-25
desktop run).  Existing libraries satisfy the new guard: the old one was stricter.
"""

DDL = """
DROP INDEX IF EXISTS assurance_blob_pin_live_uq;
CREATE UNIQUE INDEX assurance_blob_pin_object_live_uq
 ON assurance_blob_pins(mission_id,review_key,object_ref_json) WHERE state<>'RELEASED';
DROP TRIGGER IF EXISTS assurance_blob_pins_no_replace;
CREATE TRIGGER assurance_blob_pins_no_replace BEFORE INSERT ON assurance_blob_pins
WHEN EXISTS(SELECT 1 FROM assurance_blob_pins WHERE pin_id=NEW.pin_id OR (mission_id=NEW.mission_id AND review_key=NEW.review_key AND object_ref_json=NEW.object_ref_json AND state<>'RELEASED'))
BEGIN SELECT RAISE(ABORT,'duplicate identity; use original receipt'); END;
"""
