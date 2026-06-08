// =============================================================================
// Azure Intelligent Document Processing Pipeline
// main.bicep — entry point
// =============================================================================

targetScope = 'resourceGroup'

// ---------------------------------------------------------------------------
// Parameters
// ---------------------------------------------------------------------------
@description('Environment tag: dev | staging | prod')
@allowed(['dev', 'staging', 'prod'])
param envName string = 'dev'

@description('Short project prefix used in resource names (3-8 lowercase chars)')
@minLength(3)
@maxLength(8)
param projectPrefix string = 'docpipe'

@description('Azure region for all resources')
param location string = resourceGroup().location

@description('Azure OpenAI model deployment name')
param openAiModelName string = 'gpt-4.1-mini'

@description('Azure OpenAI model version')
param openAiModelVersion string = '2025-04-14'

@description('Azure OpenAI embedding model deployment name')
param embeddingModelName string = 'text-embedding-3-large'

@description('Azure OpenAI embedding model version')
param embeddingModelVersion string = '1'

@description('Your local machine public IP for firewall allowlist (e.g. 123.456.789.0)')
param allowedIpAddress string

// ---------------------------------------------------------------------------
// Variables
// ---------------------------------------------------------------------------
var suffix       = uniqueString(resourceGroup().id)
var staticWebAppLocation = 'eastus2'  // Static Web Apps not available in eastus
var shortSuffix  = substring(suffix, 0, 6)
var tags = {
  project:     'doc-intelligence-pipeline'
  environment: envName
  managedBy:   'bicep'
}

// ---------------------------------------------------------------------------
// Storage Account — Blob ingest trigger
// ---------------------------------------------------------------------------
resource storageAccount 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: '${projectPrefix}st${shortSuffix}'
  location: location
  tags: tags
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    accessTier: 'Hot'
    allowBlobPublicAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    networkAcls: {
      defaultAction: 'Deny'
      bypass: 'AzureServices'   // allows Functions + Azure services through
      ipRules: [
        { value: allowedIpAddress, action: 'Allow' }
      ]
    }
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-01-01' = {
  parent: storageAccount
  name: 'default'
}

resource ingestContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-01-01' = {
  parent: blobService
  name: 'documents-ingest'
  properties: { publicAccess: 'None' }
}

resource outputContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-01-01' = {
  parent: blobService
  name: 'documents-output'
  properties: { publicAccess: 'None' }
}

// ---------------------------------------------------------------------------
// Azure Document Intelligence (Form Recognizer)
// ---------------------------------------------------------------------------
resource documentIntelligence 'Microsoft.CognitiveServices/accounts@2023-05-01' = {
  name: '${projectPrefix}-docintel-${shortSuffix}'
  location: location
  tags: tags
  kind: 'FormRecognizer'
  sku: { name: 'S0' }  // S0 required for large documents (100+ pages)
  properties: {
    publicNetworkAccess: 'Enabled'
    customSubDomainName: '${projectPrefix}-docintel-${shortSuffix}'
    networkAcls: {
      defaultAction: 'Deny'
      ipRules: [
        { value: allowedIpAddress }
      ]
    }
  }
}

// ---------------------------------------------------------------------------
// Azure AI Vision
// ---------------------------------------------------------------------------
resource aiVision 'Microsoft.CognitiveServices/accounts@2023-05-01' = {
  name: '${projectPrefix}-vision-${shortSuffix}'
  location: location
  tags: tags
  kind: 'ComputerVision'
  sku: { name: 'F0' }
  properties: {
    publicNetworkAccess: 'Enabled'
    customSubDomainName: '${projectPrefix}-vision-${shortSuffix}'
    networkAcls: {
      defaultAction: 'Deny'
      ipRules: [
        { value: allowedIpAddress }
      ]
    }
  }
}

// ---------------------------------------------------------------------------
// Azure Translator
// ---------------------------------------------------------------------------
resource translator 'Microsoft.CognitiveServices/accounts@2023-05-01' = {
  name: '${projectPrefix}-translator-${shortSuffix}'
  location: location
  tags: tags
  kind: 'TextTranslation'
  sku: { name: 'F0' }  // Free tier: 2M chars/month
  properties: {
    publicNetworkAccess: 'Enabled'
  }
}

// ---------------------------------------------------------------------------
// Azure OpenAI
// Note: Requires approval — request access at https://aka.ms/oai/access
// ---------------------------------------------------------------------------
resource openAi 'Microsoft.CognitiveServices/accounts@2023-05-01' = {
  name: '${projectPrefix}-openai-${shortSuffix}'
  location: location
  tags: tags
  kind: 'OpenAI'
  sku: { name: 'S0' }
  properties: {
    publicNetworkAccess: 'Enabled'
    customSubDomainName: '${projectPrefix}-openai-${shortSuffix}'
    networkAcls: {
      defaultAction: 'Deny'
      ipRules: [
        { value: allowedIpAddress }
      ]
    }
    // allows Azure Functions (managed identity) through the firewall
    restrictOutboundNetworkAccess: false
  }
}

resource openAiDeployment 'Microsoft.CognitiveServices/accounts/deployments@2023-05-01' = {
  parent: openAi
  name: openAiModelName
  properties: {
    model: {
      format: 'OpenAI'
      name: openAiModelName
      version: openAiModelVersion
    }
  }
  sku: {
    name: 'Standard'
    capacity: 10  // TPM in thousands
  }
}

// ---------------------------------------------------------------------------
// Azure OpenAI — Embedding Model (text-embedding-3-large)
// Used for RAG vector index generation
// ---------------------------------------------------------------------------
resource openAiEmbeddingDeployment 'Microsoft.CognitiveServices/accounts/deployments@2023-05-01' = {
  parent: openAi
  name: embeddingModelName
  dependsOn: [openAiDeployment]  // deployments must be sequential
  properties: {
    model: {
      format: 'OpenAI'
      name: embeddingModelName
      version: embeddingModelVersion
    }
  }
  sku: {
    name: 'Standard'
    capacity: 10  // TPM in thousands
  }
}

// ---------------------------------------------------------------------------
// Azure AI Search
// ---------------------------------------------------------------------------
resource aiSearch 'Microsoft.Search/searchServices@2023-11-01' = {
  name: '${projectPrefix}-search-${shortSuffix}'
  location: location
  tags: tags
  sku: { name: 'free' }  // Free tier: 3 indexes, 50 MB
  properties: {
    replicaCount: 1
    partitionCount: 1
    publicNetworkAccess: 'enabled'
  }
}

// ---------------------------------------------------------------------------
// Azure Cosmos DB (serverless — pay per request, ~$0 at rest)
// ---------------------------------------------------------------------------
resource cosmosAccount 'Microsoft.DocumentDB/databaseAccounts@2023-11-15' = {
  name: '${projectPrefix}-cosmos-${shortSuffix}'
  location: location
  tags: tags
  kind: 'GlobalDocumentDB'
  properties: {
    databaseAccountOfferType: 'Standard'
    consistencyPolicy: { defaultConsistencyLevel: 'Session' }
    capabilities: [{ name: 'EnableServerless' }]
    locations: [{
      locationName: location
      failoverPriority: 0
      isZoneRedundant: false
    }]
    // IP firewall — your IP + Azure DC range for Functions
    ipRules: [
      { ipAddressOrRange: allowedIpAddress }
      { ipAddressOrRange: '0.0.0.0' }  // Accept connections from Azure datacenters
    ]
    publicNetworkAccess: 'Enabled'
  }
}

resource cosmosDatabase 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2023-11-15' = {
  parent: cosmosAccount
  name: 'documentpipeline'
  properties: { resource: { id: 'documentpipeline' } }
}

resource cosmosContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2023-11-15' = {
  parent: cosmosDatabase
  name: 'enriched-documents'
  properties: {
    resource: {
      id: 'enriched-documents'
      partitionKey: {
        paths: ['/documentType']
        kind: 'Hash'
      }
      indexingPolicy: {
        indexingMode: 'consistent'
        includedPaths: [{ path: '/*' }]
      }
    }
  }
}

// ---------------------------------------------------------------------------
// App Service Plan — Flex Consumption (no VM quota required)
// ---------------------------------------------------------------------------
resource appServicePlan 'Microsoft.Web/serverfarms@2023-01-01' = {
  name: '${projectPrefix}-plan-${shortSuffix}'
  location: location
  tags: tags
  sku: {
    name: 'FC1'
    tier: 'FlexConsumption'
  }
  kind: 'functionapp'
  properties: {
    reserved: true  // required for Linux
  }
}

// ---------------------------------------------------------------------------
// Application Insights + Log Analytics Workspace
// ---------------------------------------------------------------------------
resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: '${projectPrefix}-logs-${shortSuffix}'
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: '${projectPrefix}-insights-${shortSuffix}'
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
  }
}

// ---------------------------------------------------------------------------
// Azure Functions — orchestrator
// ---------------------------------------------------------------------------
resource functionApp 'Microsoft.Web/sites@2023-01-01' = {
  name: '${projectPrefix}-func-${shortSuffix}'
  location: location
  tags: tags
  kind: 'functionapp,linux'
  identity: { type: 'SystemAssigned' }
  properties: {
    serverFarmId: appServicePlan.id
    httpsOnly: true
    siteConfig: {
      linuxFxVersion: 'python|3.11'
      appSettings: [
        { name: 'AzureWebJobsStorage',                   value: 'DefaultEndpointsProtocol=https;AccountName=${storageAccount.name};EndpointSuffix=${az.environment().suffixes.storage};AccountKey=${storageAccount.listKeys().keys[0].value}' }
        { name: 'FUNCTIONS_EXTENSION_VERSION',           value: '~4' }
        { name: 'FUNCTIONS_WORKER_RUNTIME',              value: 'python' }
        { name: 'APPINSIGHTS_INSTRUMENTATIONKEY',        value: appInsights.properties.InstrumentationKey }
        { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsights.properties.ConnectionString }
        { name: 'STORAGE_ACCOUNT_NAME',                  value: storageAccount.name }
        { name: 'COSMOS_ENDPOINT',                       value: cosmosAccount.properties.documentEndpoint }
        { name: 'COSMOS_DATABASE',                       value: 'documentpipeline' }
        { name: 'COSMOS_CONTAINER',                      value: 'enriched-documents' }
        { name: 'DOC_INTEL_ENDPOINT',                    value: documentIntelligence.properties.endpoint }
        { name: 'VISION_ENDPOINT',                       value: aiVision.properties.endpoint }
        { name: 'TRANSLATOR_ENDPOINT',                   value: 'https://api.cognitive.microsofttranslator.com' }
        { name: 'OPENAI_ENDPOINT',                       value: openAi.properties.endpoint }
        { name: 'OPENAI_DEPLOYMENT',                     value: openAiModelName }
        { name: 'AI_SEARCH_ENDPOINT',                    value: 'https://${aiSearch.name}.search.windows.net' }
        { name: 'WEBSITE_RUN_FROM_PACKAGE',              value: '1' }
        { name: 'EMBEDDING_DEPLOYMENT',                  value: embeddingModelName }
        { name: 'RAG_SEARCH_INDEX',                      value: 'documents' }
        { name: 'AI_SEARCH_INDEX',                       value: 'doc-intelligence' }
        { name: 'COSMOS_KEY',                            value: cosmosAccount.listKeys().primaryMasterKey }
        { name: 'DOC_INTEL_KEY',                         value: documentIntelligence.listKeys().key1 }
        { name: 'VISION_KEY',                            value: aiVision.listKeys().key1 }
        { name: 'TRANSLATOR_KEY',                        value: translator.listKeys().key1 }
        { name: 'AI_SEARCH_KEY',                         value: aiSearch.listAdminKeys().primaryKey }
      ]
    }
  }
}

// ---------------------------------------------------------------------------
// Event Grid — blob trigger subscription
// Fires Azure Function instantly on BlobCreated in documents-ingest container
// ---------------------------------------------------------------------------
resource eventGridSubscription 'Microsoft.EventGrid/eventSubscriptions@2022-06-15' = {
  name: 'blob-ingest-trigger'
  scope: storageAccount
  dependsOn: [functionApp]
  properties: {
    destination: {
      endpointType: 'AzureFunction'
      properties: {
        resourceId: '${functionApp.id}/functions/orchestrator'
        maxEventsPerBatch: 1
        preferredBatchSizeInKilobytes: 64
      }
    }
    filter: {
      includedEventTypes: ['Microsoft.Storage.BlobCreated']
      subjectBeginsWith: '/blobServices/default/containers/documents-ingest/'
    }
    eventDeliverySchema: 'EventGridSchema'
    retryPolicy: {
      maxDeliveryAttempts: 30
      eventTimeToLiveInMinutes: 1440
    }
  }
}

// ---------------------------------------------------------------------------
// Static Web App — dashboard frontend
// Note: deploy frontend manually via VS Code extension or SWA CLI
// ---------------------------------------------------------------------------
resource staticWebApp 'Microsoft.Web/staticSites@2023-01-01' = {
  name: '${projectPrefix}-web-${shortSuffix}'
  location: staticWebAppLocation
  tags: tags
  sku: { name: 'Free', tier: 'Free' }
  properties: {}
}

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------
output storageAccountName      string = storageAccount.name
output functionAppName         string = functionApp.name
output functionAppHostname     string = functionApp.properties.defaultHostName
output staticWebAppHostname    string = staticWebApp.properties.defaultHostname
output cosmosEndpoint          string = cosmosAccount.properties.documentEndpoint
output openAiEndpoint          string = openAi.properties.endpoint
output aiSearchEndpoint        string = 'https://${aiSearch.name}.search.windows.net'
output docIntelEndpoint        string = documentIntelligence.properties.endpoint
output appInsightsKey          string = appInsights.properties.InstrumentationKey
output embeddingDeploymentName string = openAiEmbeddingDeployment.name
output eventGridSubscriptionId string = eventGridSubscription.id
