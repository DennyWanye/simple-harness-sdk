# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Published JSON Schemas for the public contract package.

The Python strict codecs remain the runtime authority; these documents are the
cross-language view of the wire shapes (H1-A2b, V2 §14).  They ship as package
data and are read through ``importlib.resources`` so neither the SDK nor its
consumers need a JSON-Schema engine.
"""
