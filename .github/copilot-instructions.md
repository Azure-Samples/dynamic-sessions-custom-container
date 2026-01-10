# Azure Container App: Microsoft Agent Framework + Azure OpenAI

Microsoft Agent Framework application with Azure OpenAI and Azure Container Apps Dynamic Sessions for secure code execution.

## Technology Stack

- **Microsoft Agent Framework**: Next-generation AI orchestration (successor to AutoGen)
- **Azure OpenAI**: GPT-3.5-turbo or GPT-4
- **Azure Container Apps**: Serverless container deployment
- **Dynamic Sessions**: Isolated code execution environments
- **Python Flask**: RESTful API with OpenAPI/Swagger

## AI Tools

1. **Weather** - Get weather for any location
2. **Tool Discovery** - List available capabilities
3. **Python Execution** - Secure code execution in isolated containers

## API Endpoints

- `POST /chat` - Main chat interface
- `POST /chat/stream` - Streaming responses
- `GET /health` - Health check
- `GET /tools` - List tools
- `DELETE /sessions/<id>` - Clear session

## Development

```bash
# Local setup
pip install -r requirements.txt
export AZURE_OPENAI_ENDPOINT="https://your-resource.openai.azure.com/"
export AZURE_OPENAI_DEPLOYMENT="gpt-35-turbo"
python main.py

# Test
python test.py
python demo.py
```

## Deployment

```bash
# Deploy everything to Azure
azd up

# Redeploy app only
azd deploy
```

## Architecture

- **ChatAgent**: Main AI orchestrator with tool capabilities
- **Type-safe Functions**: `@ai_function` decorator for automatic schema generation
- **AgentThread**: Session-based conversation management
- **Dynamic Sessions**: Secure, isolated code execution
- **Managed Identity**: Keyless Azure authentication
