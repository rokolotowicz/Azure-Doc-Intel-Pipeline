
#PowerShell script that fetches the schema (configuration/definition) of an Azure AI Search index

Invoke-RestMethod `
  -Method Get `
  -Uri "https://docpipe-search-cp3ljq.search.windows.net/indexes/documents?api-version=2024-07-01" `
  -Headers @{ "api-key" = "secret" }