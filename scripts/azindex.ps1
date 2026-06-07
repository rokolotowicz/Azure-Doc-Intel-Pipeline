$body = @'
{
  "name": "documents",
  "corsOptions": {
    "allowedOrigins": ["*"],
    "maxAgeInSeconds": 300
  },
  "fields": [
    {"name": "id", "type": "Edm.String", "key": true, "searchable": false},
    {"name": "blobName", "type": "Edm.String", "searchable": true},
    {"name": "documentType", "type": "Edm.String", "filterable": true, "facetable": true},
    {"name": "processedAt", "type": "Edm.DateTimeOffset", "filterable": true, "sortable": true},
    {"name": "searchContent", "type": "Edm.String", "searchable": true, "analyzer": "en.microsoft"},
    {"name": "sentiment", "type": "Edm.String", "filterable": true, "facetable": true},
    {"name": "summary", "type": "Edm.String", "searchable": true},
    {"name": "detectedLanguage", "type": "Edm.String", "filterable": true}
  ]
}
'@

$body | Out-File -FilePath "search-index.json" -Encoding utf8

az rest --method PUT --url "https://docpipe-search-cp3ljq.search.windows.net/indexes/documents?api-version=2024-07-01" --headers "api-key=$searchKey" "Content-Type=application/json" --skip-authorization-header --body "@search-index.json"