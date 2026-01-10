# Managed Identity Guide

This guide explains how Azure Managed Identity enables secure, credential-free authentication across services in this application.

## Overview

The Agent Framework application uses **System-Assigned Managed Identity** to authenticate with Azure services without storing credentials. This eliminates the need for connection strings, API keys, or secrets in configuration.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Azure Container App (agent-framework-app)                  │
│  ┌────────────────────────────────────────────────────────┐ │
│  │  System-Assigned Managed Identity                      │ │
│  │  Client ID: aa01bd63-XXXX-XXXX-XXXX-XXXXXXXXXXXX       │ │
│  └────────────────────────────────────────────────────────┘ │
│         │                                │                   │
│         │ (1) Request token              │ (2) Request token │
│         │ for Azure OpenAI               │ for Session Pool  │
│         ▼                                ▼                   │
└─────────────────────────────────────────────────────────────┘
          │                                │
          │ Scope:                         │ Scope:
          │ https://cognitiveservices      │ https://dynamicsessions.io
          │ .azure.com/.default            │ /.default
          │                                │
          ▼                                ▼
┌──────────────────────────────────┐    ┌──────────────────────────────────┐
│  Azure OpenAI                    │    │  Session Pool                    │
│  <your-openai-name>              │    │  <your-session-pool-name>        │
│  (agent-openai-UNIQUEID)         │    │  (agent-sessions-UNIQUEID)       │
│                                  │    │                                  │
│  Role: Cognitive Services User   │    │  Role: Session Executor          │
└──────────────────────────────────┘    └──────────────────────────────────┘
```

## How It Works

### 1. Identity Assignment

When the Container App is deployed, Azure automatically creates a managed identity:

```bash
# Bicep configuration (infra/main.bicep)
identity: {
  type: 'SystemAssigned'
}
```

This identity is tied to the Container App's lifecycle - it's created when the app is deployed and deleted when the app is deleted.

### 2. Token Acquisition

The application uses `DefaultAzureCredential` from Azure Identity SDK:

```python
from azure.identity import DefaultAzureCredential

# Automatically discovers and uses managed identity
credential = DefaultAzureCredential()

# Get token for Azure OpenAI
openai_token = credential.get_token("https://cognitiveservices.azure.com/.default")

# Get token for Session Pool
session_token = credential.get_token("https://dynamicsessions.io/.default")
```

**How DefaultAzureCredential works:**
1. Tries managed identity first (in Azure environments)
2. Falls back to Azure CLI credentials (local development)
3. Tries Visual Studio Code credentials
4. Tries other authentication methods in sequence

### 3. Service Authentication

#### Azure OpenAI Connection

```python
# main.py (lines 51-63)
from azure.ai.inference import ChatCompletionsClient
from azure.identity import DefaultAzureCredential

credential = DefaultAzureCredential()
chat_client = ChatCompletionsClient(
    endpoint=AZURE_OPENAI_ENDPOINT,
    credential=credential
)
```

**No API key needed** - the managed identity authenticates automatically.

**Required Role Assignment:**
```bash
# Assign "Cognitive Services User" role to managed identity
az role assignment create \
  --assignee <managed-identity-client-id> \
  --role "Cognitive Services User" \
  --scope /subscriptions/<subscription-id>/resourceGroups/<your-resource-group>/providers/Microsoft.CognitiveServices/accounts/<your-openai-name>
```

#### Session Pool Connection

```python
# main.py (lines 265-279)
credential = DefaultAzureCredential()
token = credential.get_token(SESSION_POOL_AUDIENCE)

headers = {
    "Authorization": f"Bearer {token.token}",
    "Content-Type": "application/json"
}

response = requests.post(
    f"{SESSION_POOL_ENDPOINT}/execute?identifier={session_id}",
    headers=headers,
    json=payload
)
```

**Token Audience:** `https://dynamicsessions.io/.default`

**Required Role Assignment:**
```bash
# Assign Azure Container Apps Session Executor role to managed identity
az role assignment create \
  --assignee <managed-identity-client-id> \
  --role "Azure Container Apps Session Executor" \
  --scope /subscriptions/<subscription-id>/resourceGroups/<your-resource-group>/providers/Microsoft.App/sessionPools/<your-session-pool-name>
```

## Environment Variables

The application uses these environment variables (no secrets required):

```bash
# Azure OpenAI Configuration
AZURE_OPENAI_ENDPOINT="https://<your-openai-name>.openai.azure.com/"
AZURE_OPENAI_DEPLOYMENT="gpt-4o-mini"

# Session Pool Configuration
AZURE_CONTAINER_APPS_SESSION_POOL_ENDPOINT="https://<your-session-pool-name>.<environment-unique-id>.<region>.azurecontainerapps.io"
SESSION_POOL_AUDIENCE="https://dynamicsessions.io/.default"

# No API keys or secrets needed!
```

## Local Development

When running locally, `DefaultAzureCredential` falls back to:

### Option 1: Azure CLI Authentication
```bash
# Login with Azure CLI
az login

# Set subscription
az account set --subscription <subscription-id>

# Run the app - automatically uses Azure CLI credentials
python main.py
```

### Option 2: Service Principal (CI/CD)
```bash
# Set environment variables for service principal
export AZURE_CLIENT_ID="..."
export AZURE_CLIENT_SECRET="..."
export AZURE_TENANT_ID="..."

# Run the app
python main.py
```

### Option 3: Demo Mode
```bash
# Run without Azure credentials
# App runs in demo mode with simulated responses
python main.py
```

## Security Benefits

### ✅ No Credential Storage
- No API keys in code
- No connection strings in configuration
- No secrets in environment variables
- No credentials in version control

### ✅ Automatic Rotation
- Azure manages token lifecycle
- Tokens automatically renewed
- No manual credential rotation needed

### ✅ Least Privilege
- Each service gets minimal required permissions
- Fine-grained role assignments
- Audit trail in Azure Activity Log

### ✅ Reduced Attack Surface
- No credentials to leak or steal
- No hardcoded secrets in code
- Credentials never leave Azure platform

## Role Assignments Required

The managed identity needs these roles:

| Service | Role | Scope | Purpose |
|---------|------|-------|------|
| Azure OpenAI | Cognitive Services User | OpenAI resource | Generate AI responses |
| Session Pool | Azure Container Apps Session Executor | Session Pool resource | Execute Python code in dynamic sessions |

## Troubleshooting

### Issue: "DefaultAzureCredential failed to retrieve a token"

**Local Development:**
```bash
# Ensure you're logged in
az login
az account show

# Verify you can get a token
az account get-access-token --resource https://cognitiveservices.azure.com
```

**Azure Deployment:**
```bash
# Verify managed identity is enabled
az containerapp show \
  --name <your-container-app-name> \
  --resource-group <your-resource-group> \
  --query "identity"

# Check role assignments
az role assignment list \
  --assignee <managed-identity-client-id> \
  --output table
```

### Issue: "403 Forbidden" from Azure OpenAI

**Solution:** Add role assignment:
```bash
# Get managed identity principal ID
PRINCIPAL_ID=$(az containerapp show \
  --name <your-container-app-name> \
  --resource-group <your-resource-group> \
  --query "identity.principalId" -o tsv)

# Assign Cognitive Services User role
az role assignment create \
  --assignee $PRINCIPAL_ID \
  --role "Cognitive Services User" \
  --scope /subscriptions/<subscription-id>/resourceGroups/<your-resource-group>/providers/Microsoft.CognitiveServices/accounts/<your-openai-name>
```

### Issue: "401 Unauthorized" from Session Pool

**Solution:** Verify token audience and role:
```bash
# Test token acquisition
az account get-access-token --resource https://dynamicsessions.io

# Check session pool access
az containerapp sessionpool show \
  --name <your-session-pool-name> \
  --resource-group <your-resource-group>
```

## Token Flow Example

```python
# 1. Application starts
credential = DefaultAzureCredential()

# 2. Request to Azure OpenAI
# DefaultAzureCredential detects managed identity
# Contacts Azure Instance Metadata Service (IMDS)
# IMDS returns token for cognitiveservices.azure.com
chat_client = ChatCompletionsClient(endpoint=..., credential=credential)

# 3. Request to Session Pool
# Get token with specific audience
token = credential.get_token("https://dynamicsessions.io/.default")

# 4. Token contains:
# - Issued for: https://dynamicsessions.io
# - Subject: Managed identity principal ID
# - Claims: Role assignments
# - Expiration: ~1 hour (automatically refreshed)

# 5. Azure service validates token
# - Verifies signature
# - Checks expiration
# - Validates role assignments
# - Grants or denies access
```

## Best Practices

1. **Use System-Assigned Identity** - Lifecycle managed with resource
2. **Follow Least Privilege** - Grant minimal required roles
3. **Use DefaultAzureCredential** - Works everywhere (local + Azure)
4. **Specify Token Audience** - Use correct scope for each service
5. **Monitor Role Assignments** - Review periodically for security
6. **Test Locally with Azure CLI** - Same credential flow as production

## Verification Commands

```bash
# Check managed identity status
az containerapp show \
  --name <your-container-app-name> \
  --resource-group <your-resource-group> \
  --query "{identity:identity.type, principalId:identity.principalId}" \
  --output table

# List all role assignments
az role assignment list \
  --assignee $(az containerapp show --name <your-container-app-name> --resource-group <your-resource-group> --query "identity.principalId" -o tsv) \
  --all \
  --output table

# Test token acquisition locally
az account get-access-token --resource https://cognitiveservices.azure.com
az account get-access-token --resource https://dynamicsessions.io

# Check app logs for authentication issues
az containerapp logs show \
  --name <your-container-app-name> \
  --resource-group <your-resource-group> \
  --tail 50 \
  | Select-String "credential|token|authentication|401|403"
```

## Additional Resources

- [Azure Managed Identity Documentation](https://learn.microsoft.com/azure/active-directory/managed-identities-azure-resources/)
- [DefaultAzureCredential Overview](https://learn.microsoft.com/python/api/azure-identity/azure.identity.defaultazurecredential)
- [Azure Container Apps Managed Identity](https://learn.microsoft.com/azure/container-apps/managed-identity)
- [Azure RBAC Role Assignments](https://learn.microsoft.com/azure/role-based-access-control/role-assignments)
