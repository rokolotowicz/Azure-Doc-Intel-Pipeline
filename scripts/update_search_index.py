#!/usr/bin/env python3
"""
scripts/update_search_index.py
──────────────────────────────
Creates the 'documents' index from scratch if it does not exist,
or updates it if it does. This index is optimized for vector/semantic RAG.

Usage:
    pip install azure-search-documents==11.6.0b5 python-dotenv
    python scripts/update_search_index.py
"""

import os
import sys
from dotenv import load_dotenv

load_dotenv()

from azure.core.credentials import AzureKeyCredential
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (
    SearchIndex,
    SearchField,
    SearchFieldDataType,
    SimpleField,
    SearchableField,
    VectorSearch,
    HnswAlgorithmConfiguration,
    VectorSearchProfile,
    SemanticConfiguration,
    SemanticSearch,
    SemanticPrioritizedFields,
    SemanticField,
)

SEARCH_ENDPOINT  = os.environ["AZURE_SEARCH_ENDPOINT"]
SEARCH_API_KEY   = os.environ["AZURE_SEARCH_API_KEY"]
INDEX_NAME       = os.environ.get("AZURE_SEARCH_INDEX_NAME", "documents")
VECTOR_DIMS      = 3072   # text-embedding-3-large output dimensions


def main():
    client = SearchIndexClient(
        endpoint=SEARCH_ENDPOINT,
        credential=AzureKeyCredential(SEARCH_API_KEY),
    )

    # ── Define Vector Search Profile ──────────────────────────
    vector_search = VectorSearch(
        algorithms=[
            HnswAlgorithmConfiguration(
                name="rag-hnsw",
                parameters={"m": 4, "efConstruction": 400, "efSearch": 500, "metric": "cosine"},
            )
        ],
        profiles=[
            VectorSearchProfile(name="rag-hnsw-profile", algorithm_configuration_name="rag-hnsw")
        ],
    )

    # ── Define Semantic Configuration ─────────────────────────
    semantic_search = SemanticSearch(
        configurations=[
            SemanticConfiguration(
                name="default",
                prioritized_fields=SemanticPrioritizedFields(
                    content_fields=[SemanticField(field_name="searchContent")],
                    keywords_fields=[
                        SemanticField(field_name="blobName"),
                        SemanticField(field_name="documentType"),
                    ],
                ),
            )
        ]
    )

    # ── Complete Schema Definition (Dashboard + RAG) ──────────
    fields = [
        SimpleField(name="id", type=SearchFieldDataType.String, key=True),
        SearchableField(name="blobName", type=SearchFieldDataType.String, filterable=True, sortable=True),
        SearchableField(name="documentName", type=SearchFieldDataType.String, filterable=True, sortable=True),
        SimpleField(name="documentType", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SimpleField(name="processedAt", type=SearchFieldDataType.DateTimeOffset, filterable=True, sortable=True),
        SimpleField(name="enrichedAt", type=SearchFieldDataType.DateTimeOffset, filterable=True, sortable=True),
        SearchableField(name="searchContent", type=SearchFieldDataType.String, analyzer_name="en.microsoft"),
        SimpleField(name="sentiment", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SearchableField(name="summary", type=SearchFieldDataType.String),
        SimpleField(name="pageNumber", type=SearchFieldDataType.Int32, filterable=True, sortable=True),
        SimpleField(name="sourcePath", type=SearchFieldDataType.String),
        SearchField(
            name="content_vector",
            type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
            searchable=True,
            vector_search_dimensions=VECTOR_DIMS,
            vector_search_profile_name="rag-hnsw-profile",
        )
    ]

    index_definition = SearchIndex(
        name=INDEX_NAME,
        fields=fields,
        vector_search=vector_search,
        semantic_search=semantic_search,
    )

    # ── Check and Execute ─────────────────────────────────────
    try:
        client.get_index(INDEX_NAME)
        print(f"✓ Found existing index '{INDEX_NAME}'. Updating schema...")
    except Exception:
        print(f"✦ Index '{INDEX_NAME}' not found. Creating a fresh vector index...")

    try:
        client.create_or_update_index(index_definition)
        print(f"✓ Vector Index '{INDEX_NAME}' created or updated successfully.")
        print(f"  Vector dims:    {VECTOR_DIMS}")
        print(f"  Semantic config: default")
    except Exception as e:
        print(f"✗ Failed to create or update index: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()