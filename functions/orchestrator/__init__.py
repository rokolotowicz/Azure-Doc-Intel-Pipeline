"""
Azure Intelligent Document Processing Pipeline
Orchestrator Function — triggered on blob upload to documents-ingest container

Flow:
  1. Blob upload triggers this function
  2. Fan out to 4 AI services in parallel (asyncio)
     - Azure Document Intelligence → layout, key-value pairs, tables
     - Azure AI Vision            → captions, tags, objects
     - Azure OpenAI               → summary, classification, key entities
     - Azure Translator           → detect language, translate to English
  3. Merge all enrichment results
  4. Write to Cosmos DB (structured storage)
  5. Index in Azure AI Search (semantic search)
  6. Log telemetry to Application Insights
"""

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone

import azure.functions as func
from azure.ai.formrecognizer import DocumentAnalysisClient
from azure.ai.vision.imageanalysis import ImageAnalysisClient
from azure.ai.vision.imageanalysis.models import VisualFeatures
from azure.core.credentials import AzureKeyCredential
from azure.cosmos import CosmosClient
from azure.search.documents import SearchClient
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
)
from openai import AzureOpenAI

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger("doc-pipeline")
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Environment config (populated by Bicep via Function App settings)
# ---------------------------------------------------------------------------
DOC_INTEL_ENDPOINT   = os.environ["DOC_INTEL_ENDPOINT"]
DOC_INTEL_KEY        = os.environ["DOC_INTEL_KEY"]

VISION_ENDPOINT      = os.environ["VISION_ENDPOINT"]
VISION_KEY           = os.environ["VISION_KEY"]

TRANSLATOR_ENDPOINT  = os.environ["TRANSLATOR_ENDPOINT"]
TRANSLATOR_KEY       = os.environ["TRANSLATOR_KEY"]
TRANSLATOR_REGION    = os.environ.get("TRANSLATOR_REGION", "eastus")

OPENAI_ENDPOINT      = os.environ["OPENAI_ENDPOINT"]
OPENAI_KEY           = os.environ["OPENAI_KEY"]
OPENAI_DEPLOYMENT    = os.environ.get("OPENAI_DEPLOYMENT", "gpt-4.1-mini")

COSMOS_ENDPOINT      = os.environ["COSMOS_ENDPOINT"]
COSMOS_KEY           = os.environ["COSMOS_KEY"]
COSMOS_DATABASE      = os.environ.get("COSMOS_DATABASE", "documentpipeline")
COSMOS_CONTAINER     = os.environ.get("COSMOS_CONTAINER", "enriched-documents")

AI_SEARCH_ENDPOINT   = os.environ["AI_SEARCH_ENDPOINT"]
AI_SEARCH_KEY        = os.environ["AI_SEARCH_KEY"]
AI_SEARCH_INDEX      = os.environ.get("AI_SEARCH_INDEX", "documents")

# ---------------------------------------------------------------------------
# Blob trigger entry point
# ---------------------------------------------------------------------------
async def main(myblob: func.InputStream) -> None:
    """
    Triggered when a file is uploaded to the documents-ingest container.
    myblob.name  = container/filename  e.g. documents-ingest/invoice.pdf
    myblob.read() = raw bytes of the file
    """
    blob_name = myblob.name
    blob_content = myblob.read()

    logger.info(f"[pipeline] triggered for blob: {blob_name} ({len(blob_content)} bytes)")

    # Determine file type
    file_ext = blob_name.rsplit(".", 1)[-1].lower() if "." in blob_name else "unknown"
    is_image = file_ext in {"jpg", "jpeg", "png", "bmp", "tiff", "webp"}
    is_document = file_ext in {"pdf", "docx", "xlsx", "pptx"}

    # Run all AI enrichments in parallel
    results = await asyncio.gather(
        enrich_document_intelligence(blob_content, blob_name),
        enrich_vision(blob_content, blob_name, is_image),
        enrich_openai(blob_content, blob_name, file_ext),
        enrich_translator(blob_content, blob_name, file_ext),
        return_exceptions=True  # don't fail pipeline if one service errors
    )

    doc_intel_result, vision_result, openai_result, translator_result = results

    # Log any service-level failures without crashing the pipeline
    for name, result in [
        ("DocumentIntelligence", doc_intel_result),
        ("Vision", vision_result),
        ("OpenAI", openai_result),
        ("Translator", translator_result),
    ]:
        if isinstance(result, Exception):
            logger.warning(f"[pipeline] {name} failed: {result}")

    # Build enriched document
    doc_id = str(uuid.uuid4())
    enriched = build_enriched_document(
        doc_id=doc_id,
        blob_name=blob_name,
        file_ext=file_ext,
        doc_intel=doc_intel_result if not isinstance(doc_intel_result, Exception) else None,
        vision=vision_result if not isinstance(vision_result, Exception) else None,
        openai=openai_result if not isinstance(openai_result, Exception) else None,
        translator=translator_result if not isinstance(translator_result, Exception) else None,
    )

    # Write to Cosmos DB and AI Search in parallel
    await asyncio.gather(
        write_cosmos(enriched),
        write_search_index(enriched),
        return_exceptions=True
    )

    logger.info(f"[pipeline] completed for {blob_name} → doc_id={doc_id}")


# ---------------------------------------------------------------------------
# AI Enrichment functions
# ---------------------------------------------------------------------------

async def enrich_document_intelligence(content: bytes, blob_name: str) -> dict:
    """Extract layout, key-value pairs, and tables using Document Intelligence."""
    logger.info(f"[doc-intel] analyzing {blob_name}")

    client = DocumentAnalysisClient(
        endpoint=DOC_INTEL_ENDPOINT,
        credential=AzureKeyCredential(DOC_INTEL_KEY)
    )

    # Use prebuilt-layout for general documents, prebuilt-invoice for invoices
    model = "prebuilt-invoice" if "invoice" in blob_name.lower() else "prebuilt-layout"

    poller = client.begin_analyze_document(model, content)
    result = poller.result()

    extracted = {
        "model_used": model,
        "pages": len(result.pages) if result.pages else 0,
        "key_value_pairs": [],
        "tables": [],
        "full_text": "",
    }

    # Extract key-value pairs (great for forms/invoices)
    if result.key_value_pairs:
        for kv in result.key_value_pairs:
            if kv.key and kv.value:
                extracted["key_value_pairs"].append({
                    "key": kv.key.content,
                    "value": kv.value.content,
                    "confidence": kv.confidence,
                })

    # Extract tables
    if result.tables:
        for table in result.tables:
            table_data = []
            for cell in table.cells:
                table_data.append({
                    "row": cell.row_index,
                    "col": cell.column_index,
                    "content": cell.content,
                })
            extracted["tables"].append(table_data)

    # Extract full text from all pages
    if result.pages:
        all_text = []
        for page in result.pages:
            if page.lines:
                all_text.extend([line.content for line in page.lines])
        extracted["full_text"] = " ".join(all_text)

    logger.info(f"[doc-intel] found {extracted['pages']} pages, "
                f"{len(extracted['key_value_pairs'])} KV pairs, "
                f"{len(extracted['tables'])} tables")
    return extracted


async def enrich_vision(content: bytes, blob_name: str, is_image: bool) -> dict:
    """Extract captions, tags, and objects using Azure AI Vision."""
    logger.info(f"[vision] analyzing {blob_name} (is_image={is_image})")

    # Vision works best on images — skip for pure PDFs
    if not is_image:
        return {"skipped": True, "reason": "not an image file"}

    client = ImageAnalysisClient(
        endpoint=VISION_ENDPOINT,
        credential=AzureKeyCredential(VISION_KEY)
    )

    result = client.analyze(
        image_data=content,
        visual_features=[
            VisualFeatures.CAPTION,
            VisualFeatures.TAGS,
            VisualFeatures.OBJECTS,
            VisualFeatures.READ,  # OCR from images
        ]
    )

    extracted = {
        "caption": None,
        "caption_confidence": None,
        "tags": [],
        "objects": [],
        "ocr_text": "",
    }

    if result.caption:
        extracted["caption"] = result.caption.text
        extracted["caption_confidence"] = result.caption.confidence

    if result.tags:
        extracted["tags"] = [
            {"name": t.name, "confidence": t.confidence}
            for t in result.tags.list
        ]

    if result.objects:
        extracted["objects"] = [
            {"name": o.tags[0].name if o.tags else "unknown", "confidence": o.tags[0].confidence if o.tags else 0}
            for o in result.objects.list
        ]

    if result.read:
        all_text = []
        for block in result.read.blocks:
            for line in block.lines:
                all_text.append(line.text)
        extracted["ocr_text"] = " ".join(all_text)

    logger.info(f"[vision] caption='{extracted['caption']}', "
                f"{len(extracted['tags'])} tags, {len(extracted['objects'])} objects")
    return extracted


async def enrich_openai(content: bytes, blob_name: str, file_ext: str) -> dict:
    """Summarize, classify, and extract entities using Azure OpenAI."""
    logger.info(f"[openai] enriching {blob_name}")

    client = AzureOpenAI(
        azure_endpoint=OPENAI_ENDPOINT,
        api_key=OPENAI_KEY,
        api_version="2024-08-01-preview"
    )

    # Use text content — decode if possible, otherwise note binary
    try:
        text_content = content.decode("utf-8", errors="ignore")[:4000]  # cap at 4K chars
    except Exception:
        text_content = f"[binary file: {blob_name}]"

    prompt = f"""Analyze this document and respond ONLY with a valid JSON object.
No preamble, no markdown, no explanation — just the JSON.

Document name: {blob_name}
Document content (first 4000 chars):
{text_content}

Return this exact JSON structure:
{{
  "summary": "2-3 sentence summary of the document",
  "document_type": "one of: invoice, contract, report, form, image, email, other",
  "key_entities": ["list", "of", "important", "named", "entities"],
  "topics": ["list", "of", "main", "topics"],
  "sentiment": "positive | negative | neutral",
  "action_items": ["any", "action", "items", "or", "deadlines", "found"],
  "language_detected": "ISO 639-1 language code e.g. en, fr, es"
}}"""

    response = client.chat.completions.create(
        model=OPENAI_DEPLOYMENT,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
        max_tokens=800,
    )

    raw = response.choices[0].message.content.strip()

    # Strip markdown fences if present
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]

    result = json.loads(raw)
    logger.info(f"[openai] type={result.get('document_type')}, "
                f"sentiment={result.get('sentiment')}, "
                f"entities={len(result.get('key_entities', []))}")
    return result


async def enrich_translator(content: bytes, blob_name: str, file_ext: str) -> dict:
    """Detect language and translate to English if needed."""
    import httpx

    logger.info(f"[translator] processing {blob_name}")

    try:
        text = content.decode("utf-8", errors="ignore")[:1000]
    except Exception:
        return {"skipped": True, "reason": "could not decode text"}

    if not text.strip():
        return {"skipped": True, "reason": "no text content"}

    headers = {
        "Ocp-Apim-Subscription-Key": TRANSLATOR_KEY,
        "Ocp-Apim-Subscription-Region": TRANSLATOR_REGION,
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient() as client:
        # Detect language
        detect_response = await client.post(
            f"{TRANSLATOR_ENDPOINT}/detect?api-version=3.0",
            headers=headers,
            json=[{"text": text[:500]}],
        )
        detect_result = detect_response.json()
        detected_language = detect_result[0].get("language", "unknown")
        detection_confidence = detect_result[0].get("score", 0)

        # Translate to English if not already English
        translated_text = None
        if detected_language != "en" and detection_confidence > 0.7:
            translate_response = await client.post(
                f"{TRANSLATOR_ENDPOINT}/translate?api-version=3.0&to=en",
                headers=headers,
                json=[{"text": text[:500]}],
            )
            translate_result = translate_response.json()
            if translate_result and translate_result[0].get("translations"):
                translated_text = translate_result[0]["translations"][0]["text"]

    result = {
        "detected_language": detected_language,
        "detection_confidence": detection_confidence,
        "translated_to_english": translated_text,
        "translation_required": detected_language != "en",
    }

    logger.info(f"[translator] detected={detected_language} "
                f"(confidence={detection_confidence:.2f}), "
                f"translated={translated_text is not None}")
    return result


# ---------------------------------------------------------------------------
# Build merged enriched document
# ---------------------------------------------------------------------------

def build_enriched_document(
    doc_id: str,
    blob_name: str,
    file_ext: str,
    doc_intel: dict | None,
    vision: dict | None,
    openai: dict | None,
    translator: dict | None,
) -> dict:
    """Merge all AI enrichment results into a single document."""

    # Pull the best available text
    full_text = ""
    if doc_intel and doc_intel.get("full_text"):
        full_text = doc_intel["full_text"]
    elif vision and vision.get("ocr_text"):
        full_text = vision["ocr_text"]

    return {
        # Cosmos DB requires 'id' field
        "id": doc_id,

        # Metadata
        "blobName": blob_name,
        "fileExtension": file_ext,
        "documentType": openai.get("document_type", "unknown") if openai else "unknown",
        "processedAt": datetime.now(timezone.utc).isoformat(),

        # Partition key for Cosmos DB
        "documentType": openai.get("document_type", "other") if openai else "other",

        # Document Intelligence
        "extraction": {
            "pages": doc_intel.get("pages", 0) if doc_intel else 0,
            "keyValuePairs": doc_intel.get("key_value_pairs", []) if doc_intel else [],
            "tables": doc_intel.get("tables", []) if doc_intel else [],
            "fullText": full_text,
            "modelUsed": doc_intel.get("model_used") if doc_intel else None,
        },

        # Vision
        "vision": {
            "caption": vision.get("caption") if vision else None,
            "captionConfidence": vision.get("caption_confidence") if vision else None,
            "tags": vision.get("tags", []) if vision else [],
            "objects": vision.get("objects", []) if vision else [],
            "skipped": vision.get("skipped", False) if vision else True,
        },

        # OpenAI enrichment
        "intelligence": {
            "summary": openai.get("summary") if openai else None,
            "keyEntities": openai.get("key_entities", []) if openai else [],
            "topics": openai.get("topics", []) if openai else [],
            "sentiment": openai.get("sentiment") if openai else None,
            "actionItems": openai.get("action_items", []) if openai else [],
        },

        # Translator
        "language": {
            "detected": translator.get("detected_language") if translator else None,
            "confidence": translator.get("detection_confidence") if translator else None,
            "translatedToEnglish": translator.get("translated_to_english") if translator else None,
            "translationRequired": translator.get("translation_required", False) if translator else False,
        },

        # Search-optimized combined text field
        "searchContent": " ".join(filter(None, [
            full_text,
            openai.get("summary") if openai else None,
            " ".join(openai.get("key_entities", [])) if openai else None,
            vision.get("caption") if vision else None,
            " ".join([t["name"] for t in (vision.get("tags", []) if vision else [])]),
        ])),
    }


# ---------------------------------------------------------------------------
# Cosmos DB writer
# ---------------------------------------------------------------------------

async def write_cosmos(document: dict) -> None:
    """Write enriched document to Cosmos DB."""
    logger.info(f"[cosmos] writing doc_id={document['id']}")

    client = CosmosClient(COSMOS_ENDPOINT, credential=COSMOS_KEY)
    container = client.get_database_client(COSMOS_DATABASE).get_container_client(COSMOS_CONTAINER)

    container.upsert_item(document)
    logger.info(f"[cosmos] written successfully")


# ---------------------------------------------------------------------------
# AI Search indexer
# ---------------------------------------------------------------------------

async def ensure_search_index() -> None:
    """Create AI Search index if it doesn't exist."""
    index_client = SearchIndexClient(
        endpoint=AI_SEARCH_ENDPOINT,
        credential=AzureKeyCredential(AI_SEARCH_KEY)
    )

    fields = [
        SimpleField(name="id",            type=SearchFieldDataType.String, key=True),
        SearchableField(name="blobName",  type=SearchFieldDataType.String),
        SimpleField(name="documentType",  type=SearchFieldDataType.String, filterable=True, facetable=True),
        SimpleField(name="processedAt",   type=SearchFieldDataType.DateTimeOffset, filterable=True, sortable=True),
        SearchableField(name="searchContent", type=SearchFieldDataType.String, analyzer_name="en.microsoft"),
        SimpleField(name="sentiment",     type=SearchFieldDataType.String, filterable=True, facetable=True),
        SearchableField(name="summary",   type=SearchFieldDataType.String),
        SimpleField(name="detectedLanguage", type=SearchFieldDataType.String, filterable=True),
    ]

    index = SearchIndex(name=AI_SEARCH_INDEX, fields=fields)

    try:
        index_client.create_or_update_index(index)
        logger.info(f"[search] index '{AI_SEARCH_INDEX}' ready")
    except Exception as e:
        logger.warning(f"[search] index creation warning: {e}")


async def write_search_index(document: dict) -> None:
    """Index enriched document in Azure AI Search."""
    logger.info(f"[search] indexing doc_id={document['id']}")

    await ensure_search_index()

    search_doc = {
        "id":               document["id"],
        "blobName":         document["blobName"],
        "documentType":     document["documentType"],
        "processedAt":      document["processedAt"],
        "searchContent":    document["searchContent"],
        "sentiment":        document["intelligence"].get("sentiment"),
        "summary":          document["intelligence"].get("summary"),
        "detectedLanguage": document["language"].get("detected"),
    }

    search_client = SearchClient(
        endpoint=AI_SEARCH_ENDPOINT,
        index_name=AI_SEARCH_INDEX,
        credential=AzureKeyCredential(AI_SEARCH_KEY)
    )

    search_client.upload_documents([search_doc])
    logger.info(f"[search] indexed successfully")
