#!/usr/bin/env python3
"""
scripts/create_dashboard_index.py
─────────────────────────────────
Creates or updates the dashboard (doc-intelligence) index structure.

Usage:
    pip install azure-search-documents==11.6.0b5 python-dotenv
    python scripts/create_dashboard_index.py
"""

import os
import sys
from dotenv import load_dotenv

load_dotenv()

from azure.core.credentials import AzureKeyCredential
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (
    SearchIndex,
    SimpleField,
    SearchableField,
    SearchFieldDataType,
    CorsOptions
)

SEARCH_ENDPOINT = os.environ["AZURE_SEARCH_ENDPOINT"]
SEARCH_API_KEY  = os.environ["AZURE_SEARCH_API_KEY"]
INDEX_NAME      = os.environ.get("AI_SEARCH_INDEX", "doc-intelligence")


def main():
    client = SearchIndexClient(
        endpoint=SEARCH_ENDPOINT,
        credential=AzureKeyCredential(SEARCH_API_KEY),
    )

    # Fields matching your original doc-intelligence schema precisely
    fields = [
        SimpleField(name="id", type=SearchFieldDataType.String, key=True),
        SearchableField(name="blobName", type=SearchFieldDataType.String, filterable=True, sortable=True),
        SimpleField(name="documentType", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SimpleField(name="processedAt", type=SearchFieldDataType.DateTimeOffset, filterable=True, sortable=True),
        SearchableField(name="searchContent", type=SearchFieldDataType.String, analyzer_name="en.microsoft"),
        SimpleField(name="sentiment", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SearchableField(name="summary", type=SearchFieldDataType.String),
        SimpleField(name="detectedLanguage", type=SearchFieldDataType.String, filterable=True),
    ]

    # Replicate CORS settings to allow browser-side web dashboard queries
    cors_options = CorsOptions(allowed_origins=["*"], max_age_in_seconds=300)

    index_definition = SearchIndex(
        name=INDEX_NAME,
        fields=fields,
        cors_options=cors_options
    )

    try:
        client.create_or_update_index(index_definition)
        print(f"✓ Index '{INDEX_NAME}' created or updated successfully.")
    except Exception as e:
        print(f"✗ Failed to create index: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()