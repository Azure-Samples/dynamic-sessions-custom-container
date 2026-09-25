# Observability Guide

This guide explains how to monitor, trace, and observe the Azure Container Apps Agent Framework application in development and production environments.

## Table of Contents
- [Monitoring Overview](#monitoring-overview)
- [Application Observability](#application-observability)
- [Azure Container Apps Logs](#azure-container-apps-logs)
- [Session Pool Observability](#session-pool-observability)
- [Health & Status Monitoring](#health--status-monitoring)
- [Debugging Common Issues](#debugging-common-issues)
- [Best Practices](#best-practices)

## Monitoring Overview

The Agent Framework application provides comprehensive observability through:
- **Structured logging** - All tool executions, session activities, and errors are logged
- **Health endpoints** - Real-time status of Azure OpenAI and session pool connectivity
- **Active session tracking** - Monitor all dynamic session executions and their state
- **Request tracing** - Track conversations and tool usage across the system

## Application Observability

### Local Development Monitoring

### Prerequisites
```bash
# Activate virtual environment
.\.venv\Scripts\Activate.ps1

# Install dependencies
pip install -r requirements.txt
```

### Running Locally

#### Option 1: VS Code Task
Use the "Start Agent Framework Server" task in VS Code (Terminal → Run Task)

#### Option 2: Command Line
```bash
# Set environment variables (optional for demo mode)
$env:AZURE_OPENAI_ENDPOINT = "https://your-resource.openai.azure.com/"
$env:AZURE_OPENAI_DEPLOYMENT = "gpt-5.6-sol"
$env:SESSION_POOL_ENDPOINT = "https://your-session-pool.azurecontainerapps.io"
$env:SESSION_POOL_AUDIENCE = "https://dynamicsessions.io/.default"

# Run the server
python main.py
```

The app runs at `http://localhost:8080`

### Demo Mode
If Azure OpenAI credentials are not configured, the app runs in demo mode:
- All endpoints functional
- AI responses simulated
- Perfect for testing infrastructure without Azure credentials

## Health & Status Monitoring

### Observability Endpoints

The application exposes several endpoints for monitoring and observability:

#### Health Check Endpoint
```bash
curl http://localhost:8080/api/system/health
```

**Expected Response:**
```json
{
  "status": "healthy",
  "azure_openai_configured": true,
  "session_pool_configured": true,
  "model": "gpt-5.6-sol",
  "timestamp": "2025-12-07T12:34:56.789Z"
}
```

### List Available Tools
```bash
curl http://localhost:8080/api/tools/
```

**Expected Response:**
```json
{
  "tools": [
    {
      "name": "get_weather",
      "description": "Get weather information for any location",
      "parameters": {"location": "string"}
    },
    {
      "name": "search_tools_available",
      "description": "Search and discover available AI tools",
      "parameters": {"query": "string"}
    },
    {
      "name": "execute_in_dynamic_session",
      "description": "Execute Python code in Azure Container Apps dynamic session",
      "parameters": {"code": "string"}
    }
  ]
}
```

### Test Chat Endpoint
```powershell
curl.exe -c cookies.txt -b cookies.txt `
  -X POST http://localhost:8080/api/chat/ `
  -H "Content-Type: application/json" `
  -d '{"prompt":"List the available tools"}'
```

**Expected Response:**
```json
{
  "response": "Available AI Tools: ...",
  "tools_used": [
    {
      "name": "search_tools_available",
      "icon": "🔧",
      "description": "Tool discovery"
    }
  ],
  "tools_available": ["search_tools_available", "execute_in_dynamic_session"]
}
```

### Test Python Execution
```powershell
curl.exe -c cookies.txt -b cookies.txt `
  -X POST http://localhost:8080/api/chat/ `
  -H "Content-Type: application/json" `
  -d '{"prompt":"Run this Python code: print(\"hello world\")"}'
```

**Expected Response:**
```json
{
  "response": "✅ **Code Execution Successful** (return code: 0)\n\n**Output:**\n```\nhello world\n```",
  "tools_used": [
    {
      "name": "execute_in_dynamic_session",
      "icon": "📦",
      "description": "Python Execution"
    }
  ]
}
```

The cookie jar preserves the caller's private conversation and Dynamic Session state. Session identifiers and execution records are intentionally not returned by the API.

## Debugging Common Issues

### Common Issues and Observability Solutions

### Issue 1: "No code provided" Error

**Symptoms:**
- HTTP 400 error when executing Python code
- Logs show: `"No code provided"`

**Cause:**
Session container expects code in specific format

**Solution:**
Ensure `main.py` sends code in dual format:
```python
payload = {
    "properties": {
        "codeInputType": "inline",
        "executionType": "synchronous",
        "timeoutInSeconds": 60,
        "code": code  # Nested in properties
    },
    "code": code  # Also at top level
}
```

### Issue 2: Stderr Not Captured

**Symptoms:**
- Errors occur but `last_stderr` is empty
- The agent response doesn't include the execution error

**Cause:**
Session container returns different format than expected

**Solution:**
Implement dual format handling in `main.py`:
```python
if "properties" in result:
    # Azure standard format
    stdout = result.get("properties", {}).get("stdout", "")
    stderr = result.get("properties", {}).get("stderr", "")
    return_code = result.get("properties", {}).get("returnCode", 0)
else:
    # Custom container format
    stdout = result.get("output", "")
    stderr = result.get("error", "")
    return_code = result.get("return_code", 0)
```

### Issue 3: Session Not Reused

**Symptoms:**
- New session created for each request
- Follow-up code execution doesn't see prior files or state

**Cause:**
The API client isn't retaining the opaque session cookie.

**Solution:**
Use a cookie jar for every request in the same conversation:

```powershell
curl.exe -c cookies.txt -b cookies.txt `
  -X POST http://localhost:8080/api/chat/ `
  -H "Content-Type: application/json" `
  -d '{"prompt":"Run this Python code: print(1 + 1)"}'
```

## Azure Container Apps Logs

### Understanding Log Types

Azure Container Apps provides multiple log streams for comprehensive observability:

### Deployment Issues

#### Issue: Deployment Fails
```bash
# Check deployment status
azd deploy

# If deployment fails, check the logs
az containerapp logs show `
  --name <your-container-app-name> `
  --resource-group <your-resource-group> `
  --tail 100
```

**Common deployment errors:**
- **Image pull failures**: Check ACR credentials and image exists
- **Startup failures**: Check environment variables and app startup logs
- **Resource quota**: Verify subscription has available quota

#### Issue: App Shows "Unhealthy" Status
```bash
# Check revision provisioning state
az containerapp revision list `
  --name <your-container-app-name> `
  --resource-group <your-resource-group> `
  --output table

# Check system logs for startup errors
az containerapp logs show `
  --name <your-container-app-name> `
  --resource-group <your-resource-group> `
  --type system
```

#### Issue: Changes Not Reflecting After Deployment
```bash
# Verify new revision is active
az containerapp revision list `
  --name <your-container-app-name> `
  --resource-group <your-resource-group> `
  --query "[].{Name:name, Active:properties.active, Created:properties.createdTime}" `
  --output table

# Force restart
az containerapp revision restart `
  --name <your-container-app-name> `
  --resource-group <your-resource-group> `
  --revision <revision-name>
```

### View Logs (The Right Way)

#### Application Logs (Most Important)
These show your Python app output, print statements, and errors:
```bash
# Stream application logs in real-time
az containerapp logs show `
  --name <your-container-app-name> `
  --resource-group <your-resource-group> `
  --follow

# Get last 100 lines of application logs
az containerapp logs show `
  --name <your-container-app-name> `
  --resource-group <your-resource-group> `
  --tail 100

# Search for specific errors or tool calls
az containerapp logs show `
  --name <your-container-app-name> `
  --resource-group <your-resource-group> `
  --tail 500 `
  | Select-String "ERROR|TOOL CALLED|execute_in_dynamic_session"
```

#### System Logs (For Container Issues)
These show container startup, health checks, and platform-level issues:
```bash
# View system logs (container startup, health probes)
az containerapp logs show `
  --name <your-container-app-name> `
  --resource-group <your-resource-group> `
  --type system `
  --follow

# Check for image pull or startup failures
az containerapp logs show `
  --name <your-container-app-name> `
  --resource-group <your-resource-group> `
  --type system `
  --tail 50 `
  | Select-String "pull|failed|error"
```

#### Console Logs (Structured Query)
Use Log Analytics for advanced queries:
```bash
# Get container app logs with KQL
az monitor log-analytics query `
  --workspace <workspace-id> `
  --analytics-query "ContainerAppConsoleLogs_CL | where ContainerAppName_s == '<your-container-app-name>' | order by TimeGenerated desc | take 100"
```

#### Filter by Conversation ID
Track a specific user session:
```bash
az containerapp logs show `
  --name <your-container-app-name> `
  --resource-group <your-resource-group> `
  --tail 1000 `
  | Select-String "NEW REQUEST|TOOL CALLED"
```

#### Debug Session Pool Calls
Track dynamic session execution:
```bash
# Look for session execution logs
az containerapp logs show `
  --name <your-container-app-name> `
  --resource-group <your-resource-group> `
  --tail 500 `
  | Select-String "session-|execute_in_dynamic_session|SESSION_POOL"
```

### Check App Status
```bash
# Get container app details
az containerapp show `
  --name <your-container-app-name> `
  --resource-group <your-resource-group> `
  --output table

# Check revision status
az containerapp revision list `
  --name <your-container-app-name> `
  --resource-group <your-resource-group> `
  --output table
```

### Test Deployed App
```bash
# Health check
curl https://<your-container-app-name>.<environment-unique-id>.<region>.azurecontainerapps.io/api/system/health

# Test chat while preserving the opaque session cookie
curl.exe -c cookies.txt -b cookies.txt `
  -X POST https://<your-container-app-name>.<environment-unique-id>.<region>.azurecontainerapps.io/api/chat/ `
  -H "Content-Type: application/json" `
  -d '{"prompt":"What is 2+2?"}'
```

### Check Managed Identity
```bash
# Verify managed identity has correct role assignments
az role assignment list `
  --assignee aa01bd63-XXXX-XXXX-XXXX-XXXXXXXXXXXX `
  --output table
```

## Session Pool Observability

### Monitoring Dynamic Sessions

### View Session Pool Status
```bash
az containerapp sessionpool show `
  --name <your-session-pool-name> `
  --resource-group <your-resource-group>
```

### Test Session Container Directly
```bash
# Get access token
$token = az account get-access-token --resource https://dynamicsessions.io --query accessToken -o tsv

# Execute code directly in session pool
curl -X POST "https://<your-session-pool-name>.<environment-unique-id>.<region>.azurecontainerapps.io/execute?identifier=debug-session" `
  -H "Authorization: Bearer $token" `
  -H "Content-Type: application/json" `
  -d '{
    "properties": {
      "codeInputType": "inline",
      "executionType": "synchronous",
      "timeoutInSeconds": 60,
      "code": "print(\"hello from debug\")"
    },
    "code": "print(\"hello from debug\")"
  }'
```

### Rebuild and Push Session Container
```bash
# Navigate to session container directory
cd session-container

# Build image
docker build -t azcrwk46g4pcamip2.azurecr.io/dynamic-session-executor:latest .

# Login to ACR
az acr login --name azcrwk46g4pcamip2

# Push image
docker push azcrwk46g4pcamip2.azurecr.io/dynamic-session-executor:latest

# Wait 2-3 minutes for pool to pick up new image
```

### Session Container Response Format

The session container returns this format:
```json
{
  "error": "Traceback (most recent call last)...",  // stderr
  "output": "",  // stdout
  "return_code": 1,
  "success": false,
  "execution_time": "0.123s",
  "language": "python"
}
```

**NOT** the Azure standard format with `properties` wrapper.

### Debugging Tips

### Observability Tips for Effective Debugging

### Enable Verbose Logging
Add print statements in key areas:
```python
print(f"🔍 DEBUG: Payload = {json.dumps(payload, indent=2)}")
print(f"🔍 DEBUG: Result = {json.dumps(result, indent=2)}")
print(f"🔍 DEBUG: Execution status = {status}, return code = {return_code}")
```

### Browser Console
Open browser DevTools (F12) to see:
- JavaScript errors
- Network requests/responses
- Console logs from `updateSessionPanel`

### Check Environment Variables
```python
import os
print(f"AZURE_OPENAI_ENDPOINT: {os.getenv('AZURE_OPENAI_ENDPOINT')}")
print(f"SESSION_POOL_ENDPOINT: {os.getenv('SESSION_POOL_ENDPOINT')}")
print(f"SESSION_POOL_AUDIENCE: {os.getenv('SESSION_POOL_AUDIENCE')}")
```

### Test Token Acquisition
```python
from azure.identity import DefaultAzureCredential

credential = DefaultAzureCredential()
token = credential.get_token("https://dynamicsessions.io/.default")
print(f"Token acquired: {token.token[:20]}...")
```

## Best Practices

### Observability Best Practices

1. **Enable Structured Logging**
   - Use consistent log formats with conversation IDs
   - Log all tool executions with parameters and results
   - Include timestamps and severity levels

2. **Monitor Health Endpoints**
   - Set up automated health checks every 30-60 seconds
   - Alert on `session_pool_configured: false` or `azure_openai_configured: false`
   - Track response times for performance degradation

3. **Track Active Sessions**
   - Monitor session creation and reuse patterns
   - Alert on unusually high session counts
   - Track execution times and failure rates

4. **Use Application Insights** (Optional)
   - Enable Application Insights for advanced telemetry
   - Custom metrics for tool usage and execution success rates
   - Distributed tracing across Azure services

5. **Log Retention**
   - Configure Log Analytics workspace for long-term retention
   - Set up automated log archival for compliance
   - Use diagnostic settings to export logs to storage

6. **Real-Time Monitoring**
   - Use `--follow` flag during active development
   - Set up log streaming dashboards for production
   - Configure alerts for error patterns

7. **Conversation Tracking**
   - Preserve the server-issued `HttpOnly` cookie between related requests
   - Never log or expose the opaque session identifier
   - Track end-to-end request flows with non-sensitive correlation IDs

### Logging Conventions

The application uses emoji-prefixed logging for easy visual scanning:
- 🌤️ Weather tool execution
- 🔧 Tool discovery execution
- 📦 Python execution in dynamic sessions
- ♻️ Session reuse
- 🆕 New session creation
- ✅ Successful code execution
- ❌ Failed code execution
- 🔍 Debug information

### Performance Monitoring

Key metrics to track:
- **Request latency**: Time from API call to response
- **Tool execution time**: Duration of each tool call
- **Session pool response time**: Time to execute code in dynamic sessions
- **Token usage**: Azure OpenAI API consumption
- **Error rate**: Failed requests vs total requests
- **Active sessions**: Number of concurrent dynamic sessions

### Alert Configuration Examples

```bash
# Example: Alert on high error rate
# Use Log Analytics workspace query:
# ContainerAppConsoleLogs_CL
# | where ContainerAppName_s == 'azcawk46g4pcamip2'
# | where Log_s contains "ERROR" or Log_s contains "Exception"
# | summarize ErrorCount = count() by bin(TimeGenerated, 5m)
# | where ErrorCount > 10

# Example: Alert on unhealthy status
# Use health endpoint monitoring with external service
# curl -f https://<your-container-app-name>.<environment-unique-id>.<region>.azurecontainerapps.io/api/system/health
# Alert if status != 200 or response.status != "healthy"
```

## Example Debugging Session

### Scenario 1: Deployment Issue
```bash
# 1. Deploy the app
azd deploy

# 2. If deployment succeeds but app is unhealthy, check system logs
az containerapp logs show `
  --name <your-container-app-name> `
  --resource-group <your-resource-group> `
  --type system `
  --tail 50

# 3. Check application logs for startup errors
az containerapp logs show `
  --name <your-container-app-name> `
  --resource-group <your-resource-group> `
  --tail 100

# 4. Verify environment variables are set
az containerapp show `
  --name <your-container-app-name> `
  --resource-group <your-resource-group> `
  --query "properties.template.containers[0].env"
```

### Scenario 2: Session Execution Not Working
```bash
# 1. Test the endpoint
curl.exe -c cookies.txt -b cookies.txt `
  -X POST https://<your-container-app-name>.<environment-unique-id>.<region>.azurecontainerapps.io/api/chat/ `
  -H "Content-Type: application/json" `
  -d '{"prompt":"Run: print(\"hello\")"}'

# 2. Check logs for session execution
az containerapp logs show `
  --name <your-container-app-name> `
  --resource-group <your-resource-group> `
  --tail 200 `
  | Select-String "execute_in_dynamic_session|session-|SESSION_POOL|TOOL CALLED"

# 3. Look for specific errors
az containerapp logs show `
  --name <your-container-app-name> `
  --resource-group <your-resource-group> `
  --tail 200 `
  | Select-String "ERROR|Exception|Failed|400|401|403|500"

# 4. Test session pool directly with managed identity token
$token = az account get-access-token --resource https://dynamicsessions.io --query accessToken -o tsv
curl -X POST "https://<your-session-pool-name>.<environment-unique-id>.<region>.azurecontainerapps.io/execute?identifier=test-debug" `
  -H "Authorization: Bearer $token" `
  -H "Content-Type: application/json" `
  -d '{"code": "print(\"direct test\")"}'
```

### Scenario 3: Conversation State Is Not Reused
```bash
# 1. Test the API with a persistent cookie jar
curl.exe -c cookies.txt -b cookies.txt `
  https://<your-container-app-name>.<environment-unique-id>.<region>.azurecontainerapps.io/api/chat/ `
  -X POST `
  -H "Content-Type: application/json" `
  -d '{"prompt":"Execute: print(1+1)"}' `
  | ConvertFrom-Json | ConvertTo-Json -Depth 5

# 2. Check application logs for tool execution
az containerapp logs show `
  --name <your-container-app-name> `
  --resource-group <your-resource-group> `
  --tail 100 `
  | Select-String "NEW REQUEST|execute_in_dynamic_session"
```

### Scenario 4: Local vs Azure Behavior Difference
```bash
# 1. Test locally first
python main.py
# In another terminal:
curl http://localhost:8080/api/system/health

# 2. Compare with Azure health check
curl https://<your-container-app-name>.<environment-unique-id>.<region>.azurecontainerapps.io/api/system/health

# 3. Check for environment variable differences
# Local: Uses .env or system environment
# Azure: Check with:
az containerapp show `
  --name <your-container-app-name> `
  --resource-group <your-resource-group> `
  --query "properties.template.containers[0].env" `
  --output table

# 4. Test managed identity (Azure only)
az containerapp logs show `
  --name <your-container-app-name> `
  --resource-group <your-resource-group> `
  --tail 100 `
  | Select-String "DefaultAzureCredential|token|authentication"
```

### Scenario 5: Stderr Not Captured in Error Cases
```bash
# 1. Trigger an error
curl.exe -c cookies.txt -b cookies.txt `
  -X POST https://<your-container-app-name>.<environment-unique-id>.<region>.azurecontainerapps.io/api/chat/ `
  -H "Content-Type: application/json" `
  -d '{"prompt":"Run this: 1/0"}'

# 2. Check application logs for result format
az containerapp logs show `
  --name <your-container-app-name> `
  --resource-group <your-resource-group> `
  --tail 200 `
  | Select-String "Code Execution Failed|return code" -Context 10,10

# 3. Look for session container response format
# Should show either:
#   - {"properties": {"stdout": "", "stderr": "...", "returnCode": 1}}
#   - {"output": "", "error": "...", "return_code": 1}

# 4. Verify dual format handling in code
# Check main.py has:
#   if "properties" in result:
#       stderr = result.get("properties", {}).get("stderr", "")
#   else:
#       stderr = result.get("error", "")
```

### Log Analysis Tips

**Find all tool executions:**
```bash
az containerapp logs show `
  --name <your-container-app-name> `
  --resource-group <your-resource-group> `
  --tail 1000 `
  | Select-String "TOOL CALLED"
```

**Track a specific session:**
```bash
az containerapp logs show `
  --name <your-container-app-name> `
  --resource-group <your-resource-group> `
  --tail 1000 `
  | Select-String "session-a1b2c3d4"
```

**Find authentication issues:**
```bash
az containerapp logs show `
  --name <your-container-app-name> `
  --resource-group <your-resource-group> `
  --tail 500 `
  | Select-String "401|403|Unauthorized|Forbidden|token|credential"
```

**Monitor in real-time during testing:**
```bash
# Terminal 1: Watch logs
az containerapp logs show `
  --name <your-container-app-name> `
  --resource-group <your-resource-group> `
  --follow

# Terminal 2: Send test requests
curl.exe -c cookies.txt -b cookies.txt `
  -X POST https://<your-container-app-name>.<environment-unique-id>.<region>.azurecontainerapps.io/api/chat/ `
  -H "Content-Type: application/json" `
  -d '{"prompt":"Test message"}'
```

## Additional Resources

- [Azure Container Apps Documentation](https://learn.microsoft.com/azure/container-apps/)
- [Azure Container Apps Dynamic Sessions](https://learn.microsoft.com/azure/container-apps/sessions)
- [Microsoft Agent Framework](https://github.com/microsoft/agent-framework)
- [Flask-RESTX Documentation](https://flask-restx.readthedocs.io/)
