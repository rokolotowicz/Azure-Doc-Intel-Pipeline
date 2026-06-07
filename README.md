# Azure Intelligent Document Processing Pipeline

> An end-to-end Azure AI architecture POC demonstrating intelligent document ingestion, multi-modal AI enrichment, dual-index semantic search, and a live results dashboard — built with enterprise security patterns and integrated with a companion RAG chatbot.

![Azure](https://img.shields.io/badge/Azure-AI%20Architect-0078D4?logo=microsoftazure)
![IaC](https://img.shields.io/badge/IaC-Bicep-blueviolet)
![Security](https://img.shields.io/badge/Security-Hardened-green)
![Trigger](https://img.shields.io/badge/Trigger-Event%20Grid-orange)
![Cost](https://img.shields.io/badge/POC%20Cost-~%2415--25%2Fmo-blue)

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Azure Services](#azure-services)
- [Repository Structure](#repository-structure)
- [Key Design Decisions](#key-design-decisions)
- [Quick Start](#quick-start)
- [Pipeline Cleanup & Reset](#pipeline-cleanup--reset)
- [Troubleshooting](#troubleshooting)
- [Enterprise Hardening Guide](#enterprise-hardening-guide)
- [Cost Estimate](#cost-estimate)
- [Companion Projects](#companion-projects)

---

## Overview

This POC demonstrates an Azure AI Architect's ability to design and deploy a production-shaped document intelligence pipeline across 8+ Azure services. Documents are ingested via blob storage, an Event Grid trigger fires the Azure Function, four AI services enrich the document in parallel, results are persisted to Cosmos DB, and a dual-write mechanism simultaneously indexes documents into two AI Search indexes — one for the dashboard and one for the RAG chatbot.

It pairs directly with a [Secure RAG POC](https://github.com/rokolotowicz/secure-rag-poc) to form a complete document lifecycle:

```
This repo:  ingest → enrich → index (dashboard + RAG)
RAG repo:   query  → retrieve → generate
```

---

## Architecture

```
  📄 Documents (PDF · image · invoice · form)
          │
          ▼
  🗄️  Azure Blob Storage (documents-ingest)   ← IP-restricted
          │
          │  Microsoft.Storage.BlobCreated
          ▼
  📡  Azure Event Grid                         ← instant trigger, no polling
          │
          ▼
  ⚡  Azure Functions (Flex Consumption)       ← managed identity · sequential execution
          │
          │  Sequential AI enrichment
          ├──────────────────────────────────────────┐
          ▼                                          ▼
  📋 Document Intelligence              👁️  Azure AI Vision
     Layout · KV pairs · pages              Captions · OCR · tags
          │                                          │
          ▼                                          ▼
  🤖 Azure OpenAI (gpt-4.1-mini)        🌐  Azure Translator
     Summary · classify · entities           Language detect · translate
     (Managed Identity auth)
          │
          │  Dual-write
          ├─────────────────────────────┐
          ▼                             ▼
  🔎 doc-intelligence index      🔎 documents index
     Dashboard search               RAG chatbot
     (summary · sentiment)          (chunked · vectorized)
          │                             │
          ▼                             ▼
  🌌  Azure Cosmos DB             📊  Azure Static Web App
      enriched-documents               Live dashboard
          │
          ▼
  📈  Azure Monitor + App Insights ← traces · invocations · costs
```

---

## Azure Services

| Service | Tier | Purpose |
|---|---|---|
| Azure Blob Storage | Standard LRS | Document ingest — Event Grid source |
| Azure Event Grid | Per-event billing | Instant blob trigger — replaces polling |
| Azure Functions | Flex Consumption | Orchestrator — sequential AI enrichment |
| Azure Document Intelligence | S0 | Layout, KV pairs, page-level text extraction |
| Azure AI Vision | F0 (free) | Image captions, tags, OCR |
| Azure OpenAI (`gpt-4.1-mini`) | S0 · managed identity | Summarization, classification, entities |
| Azure OpenAI (`text-embedding-3-large`) | S0 | Vector embeddings for RAG index |
| Azure Translator | F0 (free) | Language detection, translation |
| Azure AI Search | Free | Dual index — dashboard + RAG |
| Azure Cosmos DB | Serverless | Enriched document persistence |
| Azure Static Web App | Free | Dashboard frontend |
| Azure Monitor + App Insights | Pay-per-GB | Telemetry, cost tracking |

---

## Repository Structure

```
azure-doc-intelligence-pipeline/
├── infra/
│   └── main.bicep               # All 12 Azure resources as IaC
│    
├── functions/
│   ├── orchestrator/
│   │   ├── __init__.py          # Event Grid trigger + AI enrichment + dual-write
│   │   └── function.json        # Event Grid trigger binding
│   ├── host.json
│   └── requirements.txt         # Package dependencies for Function App
│   
├── scripts/
│   ├── split_pdf.py             # PDF splitter for large documents
│   ├── create_dashboard_index.py # Creates doc-intelligence AI Search index
│   └── update_search_index.py   # Creates documents RAG AI Search index
├── frontend/
│   └── index.html               # Dashboard (deployed to Static Web App)
│   
├── docs/
│   └── architecture.svg
├── deploy.yml                   # GitHub Actions CI/CD
├── index.json                   # doc-intelligence index schema (CORS-enabled)
├── .env.example
├── .gitignore
└── README.md
```

---

## Key Design Decisions

### Event Grid Trigger vs Blob Trigger
The pipeline uses an **Event Grid trigger** instead of the default blob trigger. Blob triggers use storage log polling which is unreliable on Flex Consumption plans. Event Grid delivers instant, reliable blob creation events with automatic retry (up to 30 attempts over 24 hours).

### Sequential Execution vs Parallel Threading
The function runs AI enrichment stages **sequentially on the main thread** rather than using `concurrent.futures.ThreadPoolExecutor`. Background threading suppresses Application Insights logging in the Python worker — sequential execution ensures all `logging.info()` statements appear in traces.

### Dual-Write Architecture
Each document processed writes to **two AI Search indexes simultaneously**:
- `doc-intelligence` — flat metadata for the dashboard (summary, sentiment, type)
- `documents` — chunked, vectorized pages for the RAG chatbot

This eliminates the need for backfill scripts and keeps both POCs in sync on every upload.

### Idempotent Document IDs [Future Release]
Document IDs are generated using `uuid.uuid5(uuid.NAMESPACE_DNS, blob_name)` — a deterministic UUID derived from the blob name. This prevents duplicate entries in Cosmos DB and AI Search when Event Grid retries delivery.

### OpenAI — Managed Identity
Azure OpenAI authentication uses `DefaultAzureCredential` with a bearer token provider. No API key stored anywhere. Falls back to `OPENAI_KEY` environment variable if set (for local development).

### Large Document Handling
Documents exceeding ~50 pages should be split before upload using `scripts/split_pdf.py`. The Document Intelligence S0 tier generates large JSON payloads for 100+ page documents which can exceed the 1.5GB Flex Consumption memory limit.

---

## Quick Start

### Prerequisites

- Azure CLI ≥ 2.50
- Bicep CLI — `az bicep install`
- Python 3.11 (3.12 conflicts with Azure Functions Core Tools)
- Azure Functions Core Tools v4 — `npm install -g azure-functions-core-tools@4`
- Azure Static Web Apps CLI — `npm install -g @azure/static-web-apps-cli`
- Azure OpenAI access approved — [request here](https://aka.ms/oai/access)

### 1. Deploy infrastructure

```bash
az group create --name rg-doc-intelligence-pipeline --location eastus

MY_IP=$(curl -s https://api.ipify.org)

az deployment group create \
  --resource-group rg-doc-intelligence-pipeline \
  --template-file infra/main.bicep \
  --parameters envName=dev projectPrefix=docpipe allowedIpAddress=$MY_IP
```

### 2. Register Event Grid provider

```bash
az provider register --namespace Microsoft.EventGrid

# Wait for registration
az provider show --namespace Microsoft.EventGrid --query "registrationState" --output tsv
```

### 3. Create Event Grid subscription

```bash
az eventgrid event-subscription create \
  --name blob-ingest-trigger \
  --source-resource-id /subscriptions/YOUR_SUB_ID/resourceGroups/rg-doc-intelligence-pipeline/providers/Microsoft.Storage/storageAccounts/docpipeSTSUFFIX \
  --endpoint-type azurefunction \
  --endpoint /subscriptions/YOUR_SUB_ID/resourceGroups/rg-doc-intelligence-pipeline/providers/Microsoft.Web/sites/docpipe-func-SUFFIX/functions/orchestrator \
  --included-event-types Microsoft.Storage.BlobCreated \
  --subject-begins-with /blobServices/default/containers/documents-ingest/
```

### 4. Create AI Search indexes

```bash
# Dashboard index
$env:AZURE_SEARCH_ENDPOINT = "https://docpipe-search-SUFFIX.search.windows.net"
$env:AZURE_SEARCH_API_KEY = "YOUR_ADMIN_KEY"
$env:AZURE_SEARCH_INDEX_NAME = "doc-intelligence"
python scripts/create_dashboard_index.py

# RAG index
$env:AZURE_SEARCH_INDEX_NAME = "documents"
python scripts/update_search_index.py
```

### 5. Deploy the function

```bash
# Temporarily open storage for deployment
az storage account update --name docpipeSTSUFFIX --resource-group rg-doc-intelligence-pipeline --default-action Allow

cd functions
func azure functionapp publish docpipe-func-SUFFIX --python --force

# Lock storage back down
az storage account update --name docpipeSTSUFFIX --resource-group rg-doc-intelligence-pipeline --default-action Deny
```

### 6. Deploy the dashboard

```bash
TOKEN=$(az staticwebapp secrets list --name dashboard-poc --resource-group rg-doc-intelligence-pipeline --query "properties.apiKey" -o tsv)

swa deploy ./frontend --deployment-token $TOKEN --env production
```

### 7. Test the pipeline

Upload a PDF to `documents-ingest` via the portal or CLI:

```bash
az storage blob upload \
  --account-name docpipeSTSUFFIX \
  --container-name documents-ingest \
  --file ./sample/owasp-top10.pdf \
  --name owasp-top10.pdf \
  --auth-mode key
```

Monitor execution:
```
Portal → docpipe-func-SUFFIX → Functions → orchestrator → Monitor → Invocations
```

### 8. Teardown

```bash
az group delete --name rg-doc-intelligence-pipeline --yes --no-wait
```

---

## Pipeline Cleanup & Reset

Use this procedure to fully reset the pipeline — clear all indexes, Cosmos DB, and blob storage, then reprocess all documents from scratch.

### Step 1 — Delete and recreate AI Search indexes

```powershell
$env:AZURE_SEARCH_ENDPOINT = "https://docpipe-search-cp3ljq.search.windows.net"
$env:AZURE_SEARCH_API_KEY  = "YOUR_ADMIN_KEY"

# Delete both indexes
az rest --method DELETE `
  --url "$env:AZURE_SEARCH_ENDPOINT/indexes/doc-intelligence?api-version=2024-07-01" `
  --headers "api-key=$env:AZURE_SEARCH_API_KEY" --skip-authorization-header

az rest --method DELETE `
  --url "$env:AZURE_SEARCH_ENDPOINT/indexes/documents?api-version=2024-07-01" `
  --headers "api-key=$env:AZURE_SEARCH_API_KEY" --skip-authorization-header

# Recreate indexes
$env:AZURE_SEARCH_INDEX_NAME = "doc-intelligence"
python scripts/create_dashboard_index.py

$env:AZURE_SEARCH_INDEX_NAME = "documents"
python scripts/update_search_index.py
```

### Step 2 — Clear Cosmos DB

```
Portal → docpipe-cosmos-cp3ljq
→ Data Explorer → documentpipeline → enriched-documents → Items
→ Select all → Delete
```

### Step 3 — Download, clear, and re-upload blobs

```powershell
# Download existing files
az storage blob download-batch `
  --source documents-ingest `
  --destination D:\dl\projects\Azure-Doc-Intel-Pipeline\temp-reprocess `
  --account-name docpipestcp3ljq --auth-mode login

# Delete from container
az storage blob delete-batch `
  --source documents-ingest `
  --account-name docpipestcp3ljq --auth-mode login

# Re-upload — fires Event Grid trigger on each file
az storage blob upload-batch `
  --source D:\dl\projects\Azure-Doc-Intel-Pipeline\temp-reprocess `
  --destination documents-ingest `
  --account-name docpipestcp3ljq --auth-mode login
```

---

## Troubleshooting

### Check pipeline execution logs

```kusto
traces
| where timestamp >= ago(30m)
| project timestamp, message
| order by timestamp desc
| take 50
```

### Check for exceptions

```kusto
exceptions
| where timestamp >= ago(15m)
| project timestamp, outerMessage, details
| order by timestamp desc
```

### Check failed invocations

```kusto
requests
| where timestamp >= ago(30m)
| where success == false
| project timestamp, name, success, resultCode, duration
```

### Common issues

| Symptom | Cause | Fix |
|---|---|---|
| 0 invocations after upload | Blob trigger polling broken on Flex Consumption | Use Event Grid trigger |
| Function fails in 1-17ms | Python import crash at startup | Check `exceptions` query — missing package or env var |
| Duplicate Cosmos DB entries | Event Grid retry on slow response | Use deterministic `uuid.uuid5` doc ID |
| Memory crash on large PDFs | 100+ page Document Intelligence payload exceeds 1.5GB limit | Split PDF with `scripts/split_pdf.py` before upload |
| 429 Too Many Requests | OpenAI TPM quota too low | Increase TPM in Azure OpenAI Studio, add `max_retries=5` |
| CORS error on dashboard | `ensure_search_index()` overwrote index without CORS | Index definition in code includes `CorsOptions` — redeploy |
| Storage 403 after key rotation | Local IP changed or old connection string in Function App | Update firewall rules and `AzureWebJobsStorage` app setting |
| `proxies` keyword error | httpx version conflict with Azure Functions proxy injection | Pin `httpx==0.27.2` in requirements.txt |

---

## Enterprise Hardening Guide

> The POC uses public endpoints with IP allowlisting. The patterns below replace that with a fully private, zero-trust architecture suitable for financial services, healthcare (HIPAA), or government.

### Network — VNet, Subnets, NSGs

```
VNet: 10.0.0.0/16
├── snet-functions        10.0.1.0/24   ← Function App VNet Integration
│    NSG: deny-all inbound · allow outbound to PE subnet only
├── snet-private-endpoints 10.0.2.0/24  ← all service private endpoints
│    NSG: allow inbound from functions subnet only
└── AzureBastionSubnet    10.0.3.0/27   ← admin access only
```

### Private Endpoints

| Service | Private DNS Zone |
|---|---|
| Storage | `privatelink.blob.core.windows.net` |
| Cosmos DB | `privatelink.documents.azure.com` |
| Azure OpenAI | `privatelink.openai.azure.com` |
| Document Intelligence | `privatelink.cognitiveservices.azure.com` |
| AI Search | `privatelink.search.windows.net` |
| Key Vault | `privatelink.vaultcore.azure.net` |

### VNet Integration for Azure Functions

The most commonly missed step — without this, Functions resolves cognitive service endpoints to public IPs even with private endpoints deployed:

```bicep
siteConfig: {
  vnetRouteAllEnabled: true  // forces ALL outbound through VNet
}
```

### Managed Identity — Replace All Keys

| Service | Replace with | Role |
|---|---|---|
| Storage | Managed identity | Storage Blob Data Contributor |
| Cosmos DB | Managed identity | Cosmos DB Built-in Data Contributor |
| Azure OpenAI | Managed identity ✅ (already done) | Cognitive Services OpenAI User |
| Document Intelligence | Managed identity | Cognitive Services User |
| AI Vision | Managed identity | Cognitive Services User |
| AI Search | Key Vault reference | Key Vault Secrets User |

### Key Vault

```bicep
{ name: 'AI_SEARCH_KEY', value: '@Microsoft.KeyVault(VaultName=${kvName};SecretName=ai-search-key)' }
```

Key Vault hardening: RBAC auth, soft delete (90 days), purge protection, private endpoint only.

### Monitoring & Alerting

| Alert | Threshold |
|---|---|
| OpenAI token spike | PTU > 80% for 5 min |
| Function failure rate | > 5 failures in 10 min |
| Key Vault access denied | Any 403 on secrets |
| Cosmos DB throttling | 429 rate > 1% |

---

## Cost Estimate

| Service | Tier | Monthly Cost |
|---|---|---|
| Storage | LRS hot | ~$0.02/GB |
| Event Grid | Per event | ~$0.60/1M events |
| Functions | Flex Consumption | ~$0 at low volume |
| Document Intelligence | S0 | ~$1.50/1000 pages |
| AI Vision | F0 | $0 |
| OpenAI gpt-4.1-mini | Pay-per-token | ~$1–2 |
| OpenAI text-embedding-3-large | Pay-per-token | ~$0.50 |
| AI Search | Free | $0 |
| Cosmos DB | Serverless | ~$0.25/1M RUs |
| Static Web App | Free | $0 |
| **Total** | | **~$15–25/month** |

---

## Companion Projects

- [Secure RAG POC](https://github.com/rokolotowicz/secure-rag-poc) — semantic retrieval over documents indexed by this pipeline. Shares the same AI Search service (`docpipe-search-cp3ljq`) and Azure OpenAI instance using separate indexes.

---

## Author

**Robert Okolotowicz**
AI Security Architect
[LinkedIn](https://www.linkedin.com/in/robert-okolotowicz/) · [GitHub](https://github.com/rokolotowicz)

---

## License

MIT
