# Azure Intelligent Document Processing Pipeline

> An end-to-end Azure AI architecture POC demonstrating intelligent document ingestion, multi-modal AI enrichment, semantic search, and a live results dashboard.

![Azure](https://img.shields.io/badge/Azure-AI%20Architect-0078D4?logo=microsoftazure)
![IaC](https://img.shields.io/badge/IaC-Bicep-blueviolet)
![Security](https://img.shields.io/badge/Security-Hardened-green)
![Cost](https://img.shields.io/badge/POC%20Cost-~%2415--25%2Fmo-blue)

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Azure Services](#azure-services)
- [Repository Structure](#repository-structure)
- [Quick Start](#quick-start)
- [Enterprise Hardening Guide](#enterprise-hardening-guide)
- [Cost Estimate](#cost-estimate)
- [Companion Projects](#companion-projects)

---

## Overview

This POC demonstrates an Azure AI Architect's ability to design and deploy a production-shaped document intelligence pipeline across 8+ Azure services. Documents are ingested via blob storage, enriched in parallel by four AI services, persisted to Cosmos DB, indexed in AI Search, and visualized through a deployed Static Web App dashboard.

It pairs directly with a [Secure RAG POC](https://github.com/rokolotowicz/secure-rag-poc) to form a complete document lifecycle:

```
This repo:  ingest → enrich → index
RAG repo:   query  → retrieve → generate
```

---

## Architecture

```
  📄 Documents (PDF · image · invoice · form)
          │
          ▼
  🗄️  Azure Blob Storage          ← hot tier · IP-restricted
          │  blob event trigger
          ▼
  ⚡  Azure Functions             ← Flex Consumption · managed identity
          │
    ┌─────┴──────────────────────────────────────┐
    │  asyncio.gather() — parallel fan-out       │
    ▼                                            ▼
  📋 Document Intelligence          👁️  Azure AI Vision
     Layout · KV pairs · tables         Captions · OCR · tags
    ▼                                            ▼
  🤖 Azure OpenAI (gpt-4.1-mini)    🌐  Azure Translator
     Summary · classify · entities       Language detect · translate
    │
    ▼
  🔎  Azure AI Search              ← CORS-enabled · semantic index
          │
          ▼
  🌌  Azure Cosmos DB              ← serverless · enriched JSON
          │
          ▼
  📊  Azure Static Web App         ← live results dashboard
          │
          ▼
  📈  Azure Monitor + App Insights ← observability · cost tracking
```

---

## Azure Services

| Service | Tier | Purpose |
|---|---|---|
| Azure Blob Storage | Standard LRS | Document ingest trigger |
| Azure Functions | Flex Consumption | Orchestrator — fans out to AI services |
| Azure Document Intelligence | F0 (free) | Layout, KV pairs, tables |
| Azure AI Vision | F0 (free) | Image captions, tags, OCR |
| Azure OpenAI (`gpt-4.1-mini`) | S0 · 1K TPM cap | Summarization, classification, entities |
| Azure Translator | F0 (free) | Language detection, translation |
| Azure AI Search | Free tier | Semantic search index |
| Azure Cosmos DB | Serverless | Enriched document persistence |
| Azure Static Web App | Free | Dashboard frontend |
| Azure Monitor + App Insights | Pay-per-GB | Telemetry, cost tracking |

---

## Repository Structure

```
azure-doc-intelligence-pipeline/
├── infra/
│   ├── main.bicep               # All 12 Azure resources as IaC
│   ├── v1-main.bicep            # Iteration history
│   └── ...
├── functions/
│   ├── orchestrator/
│   │   ├── __init__.py          # Blob trigger + parallel AI enrichment
│   │   └── function.json        # Blob trigger binding
│   ├── host.json                # Function runtime config
│   ├── requirements.txt         # Python dependencies
│   └── local.settings.json      # ← git-ignored, never commit
├── frontend/
│   ├── index.html               # Dashboard (deployed to Static Web App)
│   └── v1-index.html            # Iteration history
├── docs/
│   └── architecture.svg         # Architecture diagram
├── deploy.yml                   # GitHub Actions CI/CD
├── index.json                   # AI Search index schema (CORS-enabled)
├── .env.example
├── .gitignore
└── README.md
```

---

## Quick Start

### Prerequisites

- Azure CLI ≥ 2.50 — `az --version`
- Bicep CLI — `az bicep install`
- Python 3.11 (3.12 has a conflict with Azure Functions Core Tools)
- Node.js ≥ 20
- Azure Functions Core Tools v4 — `npm install -g azure-functions-core-tools@4`
- Azure Static Web Apps CLI — `npm install -g @azure/static-web-apps-cli`
- Azure OpenAI access approved — [request here](https://aka.ms/oai/access)

### 1. Clone and login

```bash
git clone https://github.com/YOUR_USERNAME/azure-doc-intelligence-pipeline
cd azure-doc-intelligence-pipeline
az login
az account set --subscription "YOUR_SUBSCRIPTION_NAME"
```

### 2. Deploy infrastructure

```bash
az group create --name rg-doc-intelligence-pipeline --location eastus

MY_IP=$(curl -s https://api.ipify.org)

az deployment group create \
  --resource-group rg-doc-intelligence-pipeline \
  --template-file infra/main.bicep \
  --parameters envName=dev projectPrefix=docpipe allowedIpAddress=$MY_IP
```

### 3. Run the function locally

```bash
# Python 3.11 venv required (3.12 conflicts with func runtime)
cd functions
python3.11 -m venv .venv
.venv\Scripts\activate        # Windows
source .venv/bin/activate     # Mac/Linux

pip install -r requirements.txt
func start
```

### 4. Upload a test document

```bash
# Via Azure Portal → Storage Accounts → docpipestcp3ljq
# → Containers → documents-ingest → Upload

# Or via CLI
az storage blob upload \
  --account-name docpipestcp3ljq \
  --container-name documents-ingest \
  --file ./sample/nist.ai.100-1.pdf \
  --name nist.ai.100-1.pdf \
  --auth-mode key
```

### 5. Deploy the frontend

```bash
# Get deployment token
az staticwebapp secrets list \
  --name dashboard-poc \
  --resource-group rg-doc-intelligence-pipeline \
  --query "properties.apiKey" -o tsv

swa deploy ./frontend \
  --deployment-token YOUR_TOKEN \
  --env production
```

### 6. Teardown

```bash
az group delete --name rg-doc-intelligence-pipeline --yes --no-wait
```

---

## Enterprise Hardening Guide

> This section documents what is required to deploy this pipeline in an enterprise environment — regulated industries such as financial services, healthcare (HIPAA), or government. The POC uses public endpoints with IP allowlisting. The patterns below replace that with a fully private, zero-trust architecture.

### Network — VNet, Subnets, NSGs

All services should communicate over private RFC 1918 address space with no public internet exposure.

```
VNet: 10.0.0.0/16
│
├── snet-functions        10.0.1.0/24   ← Function App VNet Integration
│    NSG: deny-all inbound, allow outbound to PE subnet only
│
├── snet-private-endpoints 10.0.2.0/24  ← all service private endpoints
│    NSG: allow inbound from functions subnet only, deny all else
│
└── AzureBastionSubnet    10.0.3.0/27   ← admin access only
```

**Critical NSG rule** — Functions subnet outbound must allow only the private endpoint subnet (`10.0.2.0/24`) on port 443. Deny everything else.

### Private Endpoints

Replace all public endpoints. Each service needs a private endpoint and a Private DNS Zone so the Function App resolves to the private IP automatically.

| Service | Private DNS Zone |
|---|---|
| Storage | `privatelink.blob.core.windows.net` |
| Cosmos DB | `privatelink.documents.azure.com` |
| Azure OpenAI | `privatelink.openai.azure.com` |
| Document Intelligence | `privatelink.cognitiveservices.azure.com` |
| AI Search | `privatelink.search.windows.net` |
| Key Vault | `privatelink.vaultcore.azure.net` |

> ⚠️ Disable public access **after** private endpoints and DNS zone groups are deployed — doing it before locks you out.

### VNet Integration for Azure Functions

This is the most commonly missed step. Without it, Functions resolves cognitive service endpoints to public IPs even with private endpoints deployed.

```bicep
resource functionVnetIntegration 'Microsoft.Web/sites/networkConfig@2023-01-01' = {
  parent: functionApp
  name: 'virtualNetwork'
  properties: {
    subnetResourceId: functionsSubnetId
    swiftSupported: true
  }
}
```

**Also required on the Function App:**
```bicep
siteConfig: {
  vnetRouteAllEnabled: true  // forces ALL outbound through VNet, not just RFC 1918
}
```

### Managed Identity — Replace All Keys

In the POC, service keys are stored in `local.settings.json`. In enterprise, replace with managed identity and RBAC — no secrets stored anywhere.

| Service | Replace key with | Role required |
|---|---|---|
| Storage | Managed identity | Storage Blob Data Contributor |
| Cosmos DB | Managed identity | Cosmos DB Built-in Data Contributor |
| Azure OpenAI | Managed identity | Cognitive Services OpenAI User |
| Document Intelligence | Managed identity | Cognitive Services User |
| AI Vision | Managed identity | Cognitive Services User |
| AI Search | Key Vault reference | Key Vault Secrets User |
| Translator | Key Vault reference | Key Vault Secrets User |

### Key Vault

Store the two services that don't support managed identity (AI Search, Translator) in Key Vault. Reference them from the Function App without ever exposing the key in app settings.

```bicep
// Function App app setting — secret never in plaintext
{ name: 'AI_SEARCH_KEY', value: '@Microsoft.KeyVault(VaultName=${kvName};SecretName=ai-search-key)' }
```

Key Vault hardening checklist:
- `enableRbacAuthorization: true` — use RBAC, not access policies
- `enableSoftDelete: true` with 90-day retention
- `enablePurgeProtection: true`
- `publicNetworkAccess: 'Disabled'` — private endpoint only

### Defender for Cloud

Enable at minimum:
- Defender for Storage (malware scanning on blob upload)
- Defender for Cosmos DB
- Defender for Key Vault
- Defender for App Service (covers Function App)

### Monitoring & Alerting

Send all diagnostic logs to Log Analytics. Key alerts to configure:

| Alert | Threshold |
|---|---|
| OpenAI token spike | PTU > 80% for 5 min |
| Function failure rate | > 5 failures in 10 min |
| Key Vault access denied | Any 403 on secrets |
| Cosmos DB throttling | 429 rate > 1% |

For regulated workloads, connect Log Analytics to **Microsoft Sentinel** for SIEM integration and compliance dashboards (ISO 27001, SOC 2, HIPAA).

### Data Residency

Enforce region restriction via Azure Policy to prevent resources being created outside approved regions:

```bash
az policy assignment create \
  --name "allowed-locations" \
  --policy "e56962a6-4747-49cd-b67b-bf8b01975c4f" \
  --params '{"listOfAllowedLocations":{"value":["canadacentral","canadaeast"]}}' \
  --scope /subscriptions/YOUR_SUB_ID
```

For regulated workloads also consider: Customer-Managed Keys (CMK) via Key Vault, TLS 1.3 minimum, Azure Purview data classification, and retention policies aligned to regulatory requirements.

---

## Cost Estimate

| Service | Tier | Monthly Cost |
|---|---|---|
| Storage | LRS hot | ~$0.02/GB |
| Functions | Flex Consumption | ~$0 at low volume |
| Document Intelligence | F0 | $0 (500 pages free) |
| AI Vision | F0 | $0 (5,000 calls free) |
| Translator | F0 | $0 (2M chars free) |
| OpenAI gpt-4.1-mini | 1K TPM cap | ~$0.50–2 |
| AI Search | Free | $0 |
| Cosmos DB | Serverless | ~$0.25/1M RUs |
| Static Web App | Free | $0 |
| App Insights | Pay-per-GB | ~$0 at low volume |
| **Total** | | **~$15–25/month** |

---

## Companion Projects

- [Secure RAG POC](https://github.com/rokolotowicz/secure-rag-poc)  

---

## Author

Robert Okolotowicz
Azure AI Security Architect
[LinkedIn](https://www.linkedin.com/in/robert-okolotowicz/) · [GitHub](https://github.com/rokolotowicz)

---

## License

MIT
