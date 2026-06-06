import sys; print(">>> AZURE FUNCTION WORKER IS LOADING __init__.py <<<", file=sys.stderr)

import traceback
import concurrent.futures
import json
import logging
import os
import uuid
from urllib.parse import urlparse
from datetime import datetime, timezone

import azure.functions as func

# ---------------------------------------------------------------------------
# Safe Import Block (Prevents 1ms silent crashes)
# ---------------------------------------------------------------------------
try:
    import requests
    from azure.storage.blob import BlobClient
    from azure.ai.formrecognizer import DocumentAnalysisClient
    from azure.ai.vision.imageanalysis import ImageAnalysisClient
    from azure.ai.vision.imageanalysis.models import VisualFeatures
    from azure.core.credentials import AzureKeyCredential
    from azure.cosmos import CosmosClient
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider
    from azure.search.documents import SearchClient
    from azure.search.documents.indexes import SearchIndexClient
    from azure.search.documents.indexes.models import (
        SearchIndex,
        SearchFieldDataType,
        SimpleField,
        SearchableField,
    )
    from openai import AzureOpenAI
    IMPORTS_SUCCESSFUL = True
    IMPORT_ERROR = None
    print(">>> ALL IMPORTS SUCCEEDED <<<", file=sys.stderr)
except Exception as e:
    IMPORTS_SUCCESSFUL = False
    IMPORT_ERROR = traceback.format_exc()
    print(f">>> IMPORT EXCEPTION CAUGHT: {IMPORT_ERROR} <<<", file=sys.stderr)


# ---------------------------------------------------------------------------
# Global Auth (Lazy loaded to prevent startup crashes)
# ---------------------------------------------------------------------------
_credential = None
_token_provider = None

def get_auth():
    global _credential, _token_provider
    if _credential is None:
        _credential = DefaultAzureCredential()
        _token_provider = get_bearer_token_provider(_credential, "https://cognitiveservices.azure.com/.default")
    return _credential, _token_provider


# ---------------------------------------------------------------------------
# Event Grid trigger entry point
# ---------------------------------------------------------------------------
def main(event: func.EventGridEvent) -> None:
    # 1. Absolute first logging statement inside main
    logging.info("Startup complete")
    
    # 2. Broad try/except wrapping the entire execution logic
    try:
        logging.info(f"====== PIPELINE STARTED: Event Grid Subject: {event.subject} ======")

        # Check for missing packages recorded during initial module load
        if not IMPORTS_SUCCESSFUL:
            raise RuntimeError(f"Missing Python dependency! Error:\n{IMPORT_ERROR}")

        # Safely retrieve environment variables with fallbacks
        env = {
            "DOC_INTEL_ENDPOINT": os.environ.get("DOC_INTEL_ENDPOINT"),
            "DOC_INTEL_KEY": os.environ.get("DOC_INTEL_KEY"),
            "VISION_ENDPOINT": os.environ.get("VISION_ENDPOINT"),
            "VISION_KEY": os.environ.get("VISION_KEY"),
            "TRANSLATOR_ENDPOINT": os.environ.get("TRANSLATOR_ENDPOINT"),
            "TRANSLATOR_KEY": os.environ.get("TRANSLATOR_KEY"),
            "TRANSLATOR_REGION": os.environ.get("TRANSLATOR_REGION", "eastus"),
            "OPENAI_ENDPOINT": os.environ.get("OPENAI_ENDPOINT"),
            "OPENAI_DEPLOYMENT": os.environ.get("OPENAI_DEPLOYMENT", "gpt-4.1-mini"),
            "COSMOS_ENDPOINT": os.environ.get("COSMOS_ENDPOINT"),
            "COSMOS_KEY": os.environ.get("COSMOS_KEY"),
            "COSMOS_DATABASE": os.environ.get("COSMOS_DATABASE", "documentpipeline"),
            "COSMOS_CONTAINER": os.environ.get("COSMOS_CONTAINER", "enriched-documents"),
            "AI_SEARCH_ENDPOINT": os.environ.get("AI_SEARCH_ENDPOINT"),
            "AI_SEARCH_KEY": os.environ.get("AI_SEARCH_KEY"),
            "AI_SEARCH_INDEX": os.environ.get("AI_SEARCH_INDEX", "doc-intelligence"),
            "AzureWebJobsStorage": os.environ.get("AzureWebJobsStorage"),
        }

        # Check for required properties (excluding those with built-in fallbacks)
        required_keys = [
            "DOC_INTEL_ENDPOINT", "DOC_INTEL_KEY", 
            "VISION_ENDPOINT", "VISION_KEY", 
            "TRANSLATOR_ENDPOINT", "TRANSLATOR_KEY", 
            "OPENAI_ENDPOINT", "COSMOS_ENDPOINT", "COSMOS_KEY", 
            "AI_SEARCH_ENDPOINT", "AI_SEARCH_KEY", "AzureWebJobsStorage"
        ]
        missing_vars = [k for k in required_keys if not env.get(k)]
        if missing_vars:
            raise RuntimeError(f"Missing required environment variables: {', '.join(missing_vars)}")

        # Parse Event Grid Metadata & Download Blob
        event_data = event.get_json()
        if not event_data:
            logging.warning("[pipeline] Event data is empty or malformed.")
            return

        blob_url = event_data.get('url')
        if not blob_url:
            logging.warning("[pipeline] No URL found in Event Grid data.")
            return

        logging.info(f"[pipeline] Downloading blob from URL: {blob_url}")
        
        parsed_url = urlparse(blob_url)
        path_parts = parsed_url.path.lstrip('/').split('/', 1)
        if len(path_parts) != 2:
            raise RuntimeError(f"Could not parse container and blob name from URL: {blob_url}")
            
        container_name, blob_name = path_parts[0], path_parts[1]
        
        blob_client = BlobClient.from_connection_string(
            conn_str=env["AzureWebJobsStorage"], 
            container_name=container_name, 
            blob_name=blob_name
        )
        
        blob_content = blob_client.download_blob().readall()
        logging.info(f"[pipeline] Downloaded blob: {blob_name} ({len(blob_content)} bytes)")

        file_ext = blob_name.rsplit(".", 1)[-1].lower() if "." in blob_name else "unknown"
        is_image = file_ext in {"jpg", "jpeg", "png", "bmp", "tiff", "webp"}

        # Safe executor helper to capture errors per stage without halting others
        def safe_execute(func_to_call, *args):
            try:
                return func_to_call(*args)
            except Exception as e:
                logging.error(f"[pipeline] Error in {func_to_call.__name__}: {str(e)}")
                return e

        # Parallel AI enrichment execution
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            f_doc = executor.submit(safe_execute, enrich_document_intelligence, blob_content, blob_name, env)
            f_vis = executor.submit(safe_execute, enrich_vision, blob_content, blob_name, is_image, env)
            f_oai = executor.submit(safe_execute, enrich_openai, blob_content, blob_name, file_ext, env)
            f_trn = executor.submit(safe_execute, enrich_translator, blob_content, blob_name, file_ext, env)

            doc_intel_result = f_doc.result()
            vision_result = f_vis.result()
            openai_result = f_oai.result()
            translator_result = f_trn.result()

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

        # Parallel indexing and database storage writes
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            f_cos = executor.submit(safe_execute, write_cosmos, enriched, env)
            f_src = executor.submit(safe_execute, write_search_index, enriched, env)
            
            cos_res = f_cos.result()
            src_res = f_src.result()
            if isinstance(cos_res, Exception): 
                logging.warning(f"[cosmos] failed: {cos_res}")
            if isinstance(src_res, Exception): 
                logging.warning(f"[search] failed: {src_res}")

        logging.info(f"====== PIPELINE COMPLETED for {blob_name} → doc_id={doc_id} ======")

    except Exception as e:
        # Capture and output the absolute raw stack trace to the diagnostic logs
        logging.error(f"FATAL ERROR in main: {str(e)}")
        logging.error(traceback.format_exc())
        print(f">>> FATAL EXCEPTION CAUGHT IN MAIN: {traceback.format_exc()} <<<", file=sys.stderr)


# ---------------------------------------------------------------------------
# AI Enrichment functions 
# ---------------------------------------------------------------------------

def enrich_document_intelligence(content: bytes, blob_name: str, env: dict) -> dict:
    logging.info(f"[doc-intel] analyzing {blob_name}")
    client = DocumentAnalysisClient(env["DOC_INTEL_ENDPOINT"], AzureKeyCredential(env["DOC_INTEL_KEY"]))
    model = "prebuilt-invoice" if "invoice" in blob_name.lower() else "prebuilt-layout"
    
    poller = client.begin_analyze_document(model, content)
    result = poller.result() 

    extracted = {"model_used": model, "pages": len(result.pages) if result.pages else 0, "key_value_pairs": [], "tables": [], "full_text": ""}
    if result.key_value_pairs:
        extracted["key_value_pairs"] = [{"key": kv.key.content, "value": kv.value.content, "confidence": kv.confidence} for kv in result.key_value_pairs if kv.key and kv.value]
    if result.tables:
        for table in result.tables:
            extracted["tables"].append([{"row": c.row_index, "col": c.column_index, "content": c.content} for c in table.cells])
    if result.pages:
        extracted["full_text"] = " ".join([line.content for page in result.pages if page.lines for line in page.lines])
    
    return extracted


def enrich_vision(content: bytes, blob_name: str, is_image: bool, env: dict) -> dict:
    if not is_image: 
        return {"skipped": True, "reason": "not an image"}
    client = ImageAnalysisClient(env["VISION_ENDPOINT"], AzureKeyCredential(env["VISION_KEY"]))
    
    result = client.analyze(
        image_data=content,
        visual_features=[VisualFeatures.CAPTION, VisualFeatures.TAGS, VisualFeatures.OBJECTS, VisualFeatures.READ]
    )

    extracted = {"caption": result.caption.text if result.caption else None, "caption_confidence": result.caption.confidence if result.caption else None, "tags": [], "objects": [], "ocr_text": ""}
    if result.tags: 
        extracted["tags"] = [{"name": t.name, "confidence": t.confidence} for t in result.tags.list]
    if result.objects: 
        extracted["objects"] = [{"name": o.tags[0].name if o.tags else "unknown", "confidence": o.tags[0].confidence if o.tags else 0} for o in result.objects.list]
    if result.read: 
        extracted["ocr_text"] = " ".join([line.text for block in result.read.blocks for line in block.lines])
    
    return extracted


def enrich_openai(content: bytes, blob_name: str, file_ext: str, env: dict) -> dict:
    _, token_provider = get_auth()
    client = AzureOpenAI(
        azure_endpoint=env["OPENAI_ENDPOINT"], 
        azure_ad_token_provider=token_provider, 
        api_version="2024-08-01-preview"
    )
    text_content = content.decode("utf-8", errors="ignore")[:4000] if "pdf" in file_ext or "txt" in file_ext else f"[binary file: {blob_name}]"

    prompt = f"""Analyze this document and respond ONLY with a valid JSON object.
Document name: {blob_name}
Document content (first 4000 chars): {text_content}
Return this exact JSON structure: {{"summary": "...", "document_type": "...", "key_entities": [], "topics": [], "sentiment": "...", "action_items": [], "language_detected": "..."}}"""

    response = client.chat.completions.create(
        model=env["OPENAI_DEPLOYMENT"],
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
        max_tokens=800,
    )

    raw = response.choices[0].message.content.strip()
    
    # Safely extract and parse JSON block
    try:
        if raw.startswith("```json"):
            raw = raw[7:]
        elif raw.startswith("```"):
            raw = raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()
        return json.loads(raw)
    except Exception as parse_err:
        logging.warning(f"[openai] JSON parsing failed: {parse_err}. Returning raw output as fallback summary.")
        return {
            "summary": raw[:200],
            "document_type": "unknown",
            "sentiment": "unknown"
        }


def enrich_translator(content: bytes, blob_name: str, file_ext: str, env: dict) -> dict:
    try: 
        text = content.decode("utf-8", errors="ignore")[:1000]
    except Exception: 
        return {"skipped": True, "reason": "decode fail"}
    if not text.strip(): 
        return {"skipped": True, "reason": "empty"}

    headers = {
        "Ocp-Apim-Subscription-Key": env["TRANSLATOR_KEY"], 
        "Ocp-Apim-Subscription-Region": env["TRANSLATOR_REGION"], 
        "Content-Type": "application/json"
    }
    
    detect_res = requests.post(
        f"{env['TRANSLATOR_ENDPOINT']}/detect?api-version=3.0", 
        headers=headers, 
        json=[{"text": text[:500]}]
    )
    detect_res.raise_for_status()
    detect_result = detect_res.json()
    
    detected_language = detect_result[0].get("language", "unknown")
    detection_confidence = detect_result[0].get("score", 0)
    translated_text = None

    if detected_language != "en" and detection_confidence > 0.7:
        trans_res = requests.post(
            f"{env['TRANSLATOR_ENDPOINT']}/translate?api-version=3.0&to=en", 
            headers=headers, 
            json=[{"text": text[:500]}]
        )
        if trans_res.status_code == 200 and trans_res.json()[0].get("translations"):
            translated_text = trans_res.json()[0]["translations"][0]["text"]

    return {
        "detected_language": detected_language,
        "detection_confidence": detection_confidence,
        "translated_to_english": translated_text,
        "translation_required": detected_language != "en",
    }


def build_enriched_document(doc_id, blob_name, file_ext, doc_intel, vision, openai, translator):
    # Use full text from Doc Intel, fallback to Vision OCR text
    full_text = ""
    if doc_intel and doc_intel.get("full_text"):
        full_text = doc_intel.get("full_text")
    elif vision and vision.get("ocr_text"):
        full_text = vision.get("ocr_text")

    return {
        "id": doc_id, 
        "blobName": blob_name, 
        "documentType": openai.get("document_type", "other") if openai else "other",
        "processedAt": datetime.now(timezone.utc).isoformat(),
        "extraction": {"fullText": full_text},
        "intelligence": {
            "summary": openai.get("summary") if openai else None, 
            "sentiment": openai.get("sentiment") if openai else None
        },
        "language": {
            "detected": translator.get("detected_language") if translator else None
        },
        "searchContent": full_text
    }


def write_cosmos(document: dict, env: dict) -> None:
    client = CosmosClient(env["COSMOS_ENDPOINT"], credential=env["COSMOS_KEY"])
    client.get_database_client(env["COSMOS_DATABASE"]).get_container_client(env["COSMOS_CONTAINER"]).upsert_item(document)


def write_search_index(document: dict, env: dict) -> None:
    index_client = SearchIndexClient(
        endpoint=env["AI_SEARCH_ENDPOINT"], 
        credential=AzureKeyCredential(env["AI_SEARCH_KEY"])
    )
    fields = [
        SimpleField(name="id", type=SearchFieldDataType.String, key=True),
        SearchableField(name="blobName", type=SearchFieldDataType.String),
        SimpleField(name="documentType", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SimpleField(name="processedAt", type=SearchFieldDataType.DateTimeOffset, filterable=True, sortable=True),
        SearchableField(name="searchContent", type=SearchFieldDataType.String, analyzer_name="en.microsoft"),
        SimpleField(name="sentiment", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SearchableField(name="summary", type=SearchFieldDataType.String),
        SimpleField(name="detectedLanguage", type=SearchFieldDataType.String, filterable=True),
    ]
    try: 
        index_client.create_or_update_index(SearchIndex(name=env["AI_SEARCH_INDEX"], fields=fields))
    except Exception as e:
        logging.warning(f"[search] index creation warning: {e}")
    
    search_doc = {
        "id":               document["id"],
        "blobName":         document["blobName"],
        "documentType":     document.get("documentType", "unknown"),
        "processedAt":      document.get("processedAt"),
        "searchContent":    document.get("searchContent", ""),
        "sentiment":        document.get("intelligence", {}).get("sentiment", ""),
        "summary":          document.get("intelligence", {}).get("summary", ""),
        "detectedLanguage": document.get("language", {}).get("detected", ""),
    }

    search_client = SearchClient(
        endpoint=env["AI_SEARCH_ENDPOINT"], 
        index_name=env["AI_SEARCH_INDEX"], 
        credential=AzureKeyCredential(env["AI_SEARCH_KEY"])
    )
    search_client.upload_documents([search_doc])
    logging.info(f"[search] successfully indexed {document['id']}")