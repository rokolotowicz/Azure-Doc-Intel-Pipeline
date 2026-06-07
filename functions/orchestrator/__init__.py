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
    logging.info("Startup complete")
    
    try:
        logging.info(f"====== PIPELINE STARTED: Event Grid Subject: {event.subject} ======")

        # Check for missing packages recorded during initial module load
        if not IMPORTS_SUCCESSFUL:
            raise RuntimeError(f"Missing Python dependency! Error:\n{IMPORT_ERROR}")

        # Safely retrieve environment variables with fallbacks to match RAG PoC settings
        env = {
            "DOC_INTEL_ENDPOINT": os.environ.get("DOC_INTEL_ENDPOINT"),
            "DOC_INTEL_KEY": os.environ.get("DOC_INTEL_KEY"),
            "VISION_ENDPOINT": os.environ.get("VISION_ENDPOINT"),
            "VISION_KEY": os.environ.get("VISION_KEY"),
            "TRANSLATOR_ENDPOINT": os.environ.get("TRANSLATOR_ENDPOINT"),
            "TRANSLATOR_KEY": os.environ.get("TRANSLATOR_KEY"),
            "TRANSLATOR_REGION": os.environ.get("TRANSLATOR_REGION", "eastus"),
            
            # OpenAI Fallbacks
            "OPENAI_ENDPOINT": os.environ.get("OPENAI_ENDPOINT") or os.environ.get("AZURE_OPENAI_ENDPOINT"),
            "OPENAI_KEY": os.environ.get("OPENAI_KEY") or os.environ.get("AZURE_OPENAI_API_KEY") or os.environ.get("AZURE_OPENAI_KEY"),
            "OPENAI_DEPLOYMENT": os.environ.get("OPENAI_DEPLOYMENT") or os.environ.get("GPT_DEPLOYMENT_NAME") or "gpt-4.1-mini",
            "EMBEDDING_DEPLOYMENT": os.environ.get("EMBEDDING_DEPLOYMENT") or os.environ.get("EMBEDDING_DEPLOYMENT_NAME") or "text-embedding-3-large",
            
            # Cosmos DB Configuration
            "COSMOS_ENDPOINT": os.environ.get("COSMOS_ENDPOINT"),
            "COSMOS_KEY": os.environ.get("COSMOS_KEY"),
            "COSMOS_DATABASE": os.environ.get("COSMOS_DATABASE", "documentpipeline"),
            "COSMOS_CONTAINER": os.environ.get("COSMOS_CONTAINER", "enriched-documents"),
            
            # Search Index Fallbacks
            "AI_SEARCH_ENDPOINT": os.environ.get("AI_SEARCH_ENDPOINT") or os.environ.get("AZURE_SEARCH_ENDPOINT"),
            "AI_SEARCH_KEY": os.environ.get("AI_SEARCH_KEY") or os.environ.get("AZURE_SEARCH_API_KEY"),
            "AI_SEARCH_INDEX": os.environ.get("AI_SEARCH_INDEX", "doc-intelligence"),
            "RAG_SEARCH_INDEX": os.environ.get("RAG_SEARCH_INDEX") or os.environ.get("AZURE_SEARCH_INDEX_NAME") or "documents",
            
            "AzureWebJobsStorage": os.environ.get("AzureWebJobsStorage"),
        }

        # Check for required core configurations
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

        # Pre-decode plaintext files natively
        native_text = ""
        if file_ext in {"txt", "csv", "md", "json", "html", "xml"}:
            try:
                native_text = blob_content.decode("utf-8", errors="ignore")
                logging.info(f"[pipeline] Handled plain-text file natively. Extracted {len(native_text)} characters.")
            except Exception as decode_err:
                logging.warning(f"[pipeline] Failed native decoding of plaintext file: {decode_err}")

        # Safe executor helper to capture errors per stage
        def safe_execute(func_to_call, *args):
            try:
                return func_to_call(*args)
            except Exception as e:
                logging.error(f"[pipeline] Error in {func_to_call.__name__}: {str(e)}")
                return e

        # ───────────────────────────────────────────────────────────────────────────
        # Sequential AI enrichment execution (Main Thread Debugging)
        # ───────────────────────────────────────────────────────────────────────────
        logging.info("[pipeline] Starting sequential AI enrichment execution...")
        
        doc_intel_result = safe_execute(enrich_document_intelligence, blob_content, blob_name, file_ext, env)
        vision_result = safe_execute(enrich_vision, blob_content, blob_name, is_image, env)
        openai_result = safe_execute(enrich_openai, blob_content, blob_name, file_ext, env, native_text)
        translator_result = safe_execute(enrich_translator, blob_content, blob_name, file_ext, env)
        
        logging.info("[pipeline] Sequential AI enrichment execution complete.")

        doc_id = str(uuid.uuid4())
        enriched = build_enriched_document(
            doc_id=doc_id,
            blob_name=blob_name,
            file_ext=file_ext,
            doc_intel=doc_intel_result if not isinstance(doc_intel_result, Exception) else None,
            vision=vision_result if not isinstance(vision_result, Exception) else None,
            openai=openai_result if not isinstance(openai_result, Exception) else None,
            translator=translator_result if not isinstance(translator_result, Exception) else None,
            native_text=native_text
        )

        # ───────────────────────────────────────────────────────────────────────────
        # Sequential DB/Search Index Writes (Main Thread Debugging)
        # ───────────────────────────────────────────────────────────────────────────
        logging.info("[pipeline] Starting sequential database and search index writes...")
        
        cos_res = safe_execute(write_cosmos, enriched, env)
        src_res = safe_execute(write_search_index, enriched, doc_intel_result if not isinstance(doc_intel_result, Exception) else None, native_text, env)
        
        if isinstance(cos_res, Exception): 
            logging.warning(f"[cosmos] failed: {cos_res}")
        if isinstance(src_res, Exception): 
            logging.warning(f"[search] failed: {src_res}")
            
        logging.info("[pipeline] Sequential writes complete.")

        logging.info(f"====== PIPELINE COMPLETED for {blob_name} → doc_id={doc_id} ======")

    except Exception as e:
        logging.error(f"FATAL ERROR in main: {str(e)}")
        logging.error(traceback.format_exc())
        print(f">>> FATAL EXCEPTION CAUGHT IN MAIN: {traceback.format_exc()} <<<", file=sys.stderr)


# ---------------------------------------------------------------------------
# AI Enrichment functions 
# ---------------------------------------------------------------------------

def enrich_document_intelligence(content: bytes, blob_name: str, file_ext: str, env: dict) -> dict:
    if file_ext in {"txt", "csv", "md", "json"}:
        logging.info(f"[doc-intel] Plain-text extension detected ({file_ext}). Skipping service analysis.")
        return {"skipped": True, "reason": "Plain-text file handled natively"}

    logging.info(f"[doc-intel] analyzing {blob_name}")
    client = DocumentAnalysisClient(env["DOC_INTEL_ENDPOINT"], AzureKeyCredential(env["DOC_INTEL_KEY"]))
    model = "prebuilt-invoice" if "invoice" in blob_name.lower() else "prebuilt-layout"
    
    poller = client.begin_analyze_document(model, content)
    result = poller.result() 

    # Structuring data to maintain page awareness
    extracted = {
        "model_used": model, 
        "pages_count": len(result.pages) if result.pages else 0, 
        "pages": [], 
        "key_value_pairs": [], 
        "tables": [], 
        "full_text": ""
    }
    
    if result.key_value_pairs:
        extracted["key_value_pairs"] = [{"key": kv.key.content, "value": kv.value.content, "confidence": kv.confidence} for kv in result.key_value_pairs if kv.key and kv.value]
    if result.tables:
        for table in result.tables:
            extracted["tables"].append([{"row": c.row_index, "col": c.column_index, "content": c.content} for c in table.cells])
    
    if result.pages:
        page_texts = []
        for page in result.pages:
            page_text = " ".join([line.content for line in page.lines]) if page.lines else ""
            extracted["pages"].append({
                "page_number": page.page_number,
                "text": page_text
            })
            page_texts.append(page_text)
        extracted["full_text"] = " ".join(page_texts)
    
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


def enrich_openai(content: bytes, blob_name: str, file_ext: str, env: dict, native_text: str = "") -> dict:
    openai_key = env.get("OPENAI_KEY")
    
    if openai_key:
        client = AzureOpenAI(
            azure_endpoint=env["OPENAI_ENDPOINT"], 
            api_key=openai_key, 
            api_version="2024-12-01-preview",
            max_retries=5
        )
    else:
        _, token_provider = get_auth()
        client = AzureOpenAI(
            azure_endpoint=env["OPENAI_ENDPOINT"], 
            azure_ad_token_provider=token_provider, 
            api_version="2024-12-01-preview",
            max_retries=5
        )

    text_content = native_text[:4000] if native_text else (content.decode("utf-8", errors="ignore")[:4000] if "pdf" in file_ext or "txt" in file_ext else f"[binary file: {blob_name}]")
    
    if not text_content.strip():
        return {"summary": "Empty Document", "document_type": "other", "sentiment": "neutral"}

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
            "document_type": "other",
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


def build_enriched_document(doc_id, blob_name, file_ext, doc_intel, vision, openai, translator, native_text=""):
    full_text = ""
    if native_text:
        full_text = native_text
    elif doc_intel and doc_intel.get("full_text"):
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


# ---------------------------------------------------------------------------
# Vectorization & Chunking Helpers
# ---------------------------------------------------------------------------

def chunk_text(text: str, chunk_size: int = 1000, overlap: int = 200) -> list:
    """Helper to split fallback native text into overlapping segments."""
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end])
        start += chunk_size - overlap
    return chunks


def generate_embedding(text: str, env: dict) -> list:
    """Generates 3072-dimension vectors via text-embedding-3-large."""
    openai_key = env.get("OPENAI_KEY")
    if openai_key:
        client = AzureOpenAI(
            azure_endpoint=env["OPENAI_ENDPOINT"], 
            api_key=openai_key, 
            api_version="2024-12-01-preview",
            max_retries=5
        )
    else:
        _, token_provider = get_auth()
        client = AzureOpenAI(
            azure_endpoint=env["OPENAI_ENDPOINT"], 
            azure_ad_token_provider=token_provider, 
            api_version="2024-12-01-preview",
            max_retries=5
        )
    response = client.embeddings.create(
        input=[text[:8000]], 
        model=env.get("EMBEDDING_DEPLOYMENT", "text-embedding-3-large")
    )
    return response.data[0].embedding


def write_search_index(document: dict, doc_intel: dict, native_text: str, env: dict) -> None:
    # ── WRITE 1: Dashboard Index ('doc-intelligence') ──
    dashboard_doc = {
        "id":               document["id"],
        "blobName":         document["blobName"],
        "documentType":     document.get("documentType", "unknown"),
        "processedAt":      document.get("processedAt"),
        "searchContent":    document.get("searchContent", ""),
        "sentiment":        document.get("intelligence", {}).get("sentiment", ""),
        "summary":          document.get("intelligence", {}).get("summary", ""),
        "detectedLanguage": document.get("language", {}).get("detected", ""),
    }

    dashboard_client = SearchClient(
        endpoint=env["AI_SEARCH_ENDPOINT"], 
        index_name=env["AI_SEARCH_INDEX"], 
        credential=AzureKeyCredential(env["AI_SEARCH_KEY"])
    )
    dashboard_client.upload_documents([dashboard_doc])
    logging.info(f"[search] Successfully updated dashboard index for {document['id']}")

    # ── WRITE 2: Dual Vector Index ('documents') ──
    rag_docs = []
    source_url = f"https://docpipestcp3ljq.blob.core.windows.net/documents-ingest/{document['blobName']}"

    if doc_intel and doc_intel.get("pages"):
        # Optimal PDF Path: Chunk layout-parsed text page-by-page
        for page in doc_intel["pages"]:
            page_num = page["page_number"]
            page_text = page["text"]
            if not page_text.strip():
                continue

            content_vector = []
            try:
                content_vector = generate_embedding(page_text, env)
            except Exception as emb_err:
                logging.error(f"[search] Embedding generation failed on page {page_num}: {emb_err}")

            # Safe Check: Only append if we successfully compiled a valid 3072-dim vector!
            if content_vector and len(content_vector) == 3072:
                rag_docs.append({
                    "id":             f"{document['id']}_page_{page_num}",
                    "blobName":       document["blobName"],
                    "documentName":   document["blobName"],
                    "documentType":   document.get("documentType", "unknown"),
                    "processedAt":    document.get("processedAt"),
                    "enrichedAt":     document.get("processedAt"),
                    "searchContent":  page_text,
                    "pageNumber":     int(page_num),
                    "sourcePath":     source_url,
                    "sentiment":      document.get("intelligence", {}).get("sentiment", ""),
                    "summary":        document.get("intelligence", {}).get("summary", ""),
                    "content_vector": content_vector,
                })
            else:
                logging.warning(f"[search] Skipping page {page_num} upload to RAG index because embedding was empty or invalid.")
    else:
        # Fallback Plain-Text Path: Split text string by character chunks
        fallback_text = native_text if native_text else document.get("searchContent", "")
        if fallback_text.strip():
            chunks = chunk_text(fallback_text)
            for idx, chunk in enumerate(chunks):
                content_vector = []
                try:
                    content_vector = generate_embedding(chunk, env)
                except Exception as emb_err:
                    logging.error(f"[search] Embedding generation failed on chunk {idx}: {emb_err}")

                # Safe Check: Only append if we successfully compiled a valid 3072-dim vector!
                if content_vector and len(content_vector) == 3072:
                    rag_docs.append({
                        "id":             f"{document['id']}_chunk_{idx}",
                        "blobName":       document["blobName"],
                        "documentName":   document["blobName"],
                        "documentType":   document.get("documentType", "unknown"),
                        "processedAt":    document.get("processedAt"),
                        "enrichedAt":     document.get("processedAt"),
                        "searchContent":  chunk,
                        "pageNumber":     1,
                        "sourcePath":     source_url,
                        "sentiment":      document.get("intelligence", {}).get("sentiment", ""),
                        "summary":        document.get("intelligence", {}).get("summary", ""),
                        "content_vector": content_vector,
                    })
                else:
                    logging.warning(f"[search] Skipping chunk {idx} upload to RAG index because embedding was empty or invalid.")

    # Upload vectorized segments in a batch to RAG Index
    if rag_docs:
        rag_client = SearchClient(
            endpoint=env["AI_SEARCH_ENDPOINT"], 
            index_name=env["RAG_SEARCH_INDEX"], 
            credential=AzureKeyCredential(env["AI_SEARCH_KEY"])
        )
        rag_client.upload_documents(rag_docs)
        logging.info(f"[search] Successfully chunked and vectorized {len(rag_docs)} segments into index: {env['RAG_SEARCH_INDEX']}")
    else:
        logging.warning("[search] No valid vectorized segments were compiled for RAG index.")