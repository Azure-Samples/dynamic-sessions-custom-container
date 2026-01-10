@minLength(1)
@maxLength(64)
@description('Name of the environment that can be used as part of naming resource convention')
param environmentName string = 'dev'

@minLength(1)
@description('Primary location for all resources')
param location string = 'eastus'

@description('Azure OpenAI model name to deploy')
param openAIModelName string = 'gpt-4o-mini'

@description('Azure OpenAI model version to deploy')
param openAIModelVersion string = '2024-07-18'

@description('Azure OpenAI deployment capacity')
param openAICapacity int = 30

@description('Enable Azure Container Apps dynamic sessions')
param enableDynamicSessions bool = true

@description('Maximum concurrent sessions for the session pool')
param maxConcurrentSessions int = 10

@description('Number of ready session instances to maintain')
param readySessionInstances int = 5

@description('Enable VNet integration for Container Apps Environment')
param enableVNetIntegration bool = false

@description('VNet address prefix')
param vnetAddressPrefix string = '10.0.0.0/16'

@description('Container Apps Environment subnet address prefix')
param containerAppsSubnetPrefix string = '10.0.0.0/23'

// Human-readable resource names
var resourceGroupName = resourceGroup().name
var uniqueSuffix = uniqueString(resourceGroup().id)
var managedIdentityName = 'agent-identity-${uniqueSuffix}'
var logAnalyticsName = 'agent-logs-${uniqueSuffix}'
var containerRegistryName = 'agentregistry${uniqueSuffix}'
var openAIName = 'agent-openai-${uniqueSuffix}'
var vnetName = 'agent-vnet-${uniqueSuffix}'
var containerAppsEnvName = 'agent-env-${uniqueSuffix}'
var sessionPoolName = 'agent-sessions-${uniqueSuffix}'
var containerAppName = 'agent-framework-${uniqueSuffix}'

// User-Assigned Managed Identity for secure access
resource userAssignedIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: managedIdentityName
  location: location
}

// Log Analytics Workspace for monitoring
resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logAnalyticsName
  location: location
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

// Container Registry for storing container images
resource containerRegistry 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' = {
  name: containerRegistryName
  location: location
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false
  }
}

// Azure OpenAI Service for AI capabilities
resource openAI 'Microsoft.CognitiveServices/accounts@2023-05-01' = {
  name: openAIName
  location: location
  kind: 'OpenAI'
  sku: {
    name: 'S0'
  }
  properties: {
    customSubDomainName: openAIName
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      defaultAction: 'Allow'
    }
  }
}

// GPT Model Deployment
resource gptModelDeployment 'Microsoft.CognitiveServices/accounts/deployments@2023-05-01' = {
  parent: openAI
  name: openAIModelName
  properties: {
    model: {
      format: 'OpenAI'
      name: openAIModelName
      version: openAIModelVersion
    }
    raiPolicyName: 'Microsoft.Default'
  }
  sku: {
    name: 'Standard'
    capacity: openAICapacity
  }
}

// Role assignments for secure access
resource acrPullRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: containerRegistry
  name: guid(containerRegistry.id, userAssignedIdentity.id, '7f951dda-4ed3-4680-a7ca-43fe172d538d')
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
    principalId: userAssignedIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource openAIUserRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: openAI
  name: guid(openAI.id, userAssignedIdentity.id, '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd')
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd')
    principalId: userAssignedIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// Virtual Network for Container Apps (optional)
resource vnet 'Microsoft.Network/virtualNetworks@2023-11-01' = if (enableVNetIntegration) {
  name: vnetName
  location: location
  properties: {
    addressSpace: {
      addressPrefixes: [
        vnetAddressPrefix
      ]
    }
    subnets: [
      {
        name: 'container-apps-subnet'
        properties: {
          addressPrefix: containerAppsSubnetPrefix
          delegations: [
            {
              name: 'Microsoft.App.environments'
              properties: {
                serviceName: 'Microsoft.App/environments'
              }
            }
          ]
        }
      }
    ]
  }
}

// Container Apps Environment with optional VNet
resource containerAppsEnvironment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: containerAppsEnvName
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
    vnetConfiguration: enableVNetIntegration ? {
      infrastructureSubnetId: vnet.properties.subnets[0].id
      internal: false
    } : null
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
  }
}

// Azure Container Apps Dynamic Session Pool for secure code execution
resource dynamicSessionPool 'Microsoft.App/sessionPools@2024-08-02-preview' = if (enableDynamicSessions) {
  name: sessionPoolName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${userAssignedIdentity.id}': {}
    }
  }
  properties: {
    environmentId: containerAppsEnvironment.id
    poolManagementType: 'Dynamic'
    containerType: 'CustomContainer'
    workloadProfileName: 'Consumption'
    scaleConfiguration: {
      maxConcurrentSessions: maxConcurrentSessions
      readySessionInstances: readySessionInstances
    }
    dynamicPoolConfiguration: {
      cooldownPeriodInSeconds: 300
    }
    customContainerTemplate: {
      registryCredentials: {
        server: containerRegistry.properties.loginServer
        identity: userAssignedIdentity.id
      }
      containers: [
        {
          image: '${containerRegistry.properties.loginServer}/dynamic-session-executor:latest'
          name: 'session-executor'
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: [
            {
              name: 'PYTHONUNBUFFERED'
              value: '1'
            }
          ]
        }
      ]
      ingress: {
        targetPort: 8080
      }
    }
    sessionNetworkConfiguration: {
      status: 'EgressEnabled'
    }
    managedIdentitySettings: [
      {
        identity: userAssignedIdentity.id
        lifecycle: 'Main'
      }
    ]
  }
  dependsOn: [
    acrPullRoleAssignment
  ]
}

// Role assignment for managed identity to execute sessions on the session pool
resource sessionExecutorRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (enableDynamicSessions) {
  name: guid(dynamicSessionPool.id, userAssignedIdentity.id, 'SessionExecutor')
  scope: dynamicSessionPool
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '0fb8eba5-a2bb-4abe-b1c1-49dfad359bb0') // Azure ContainerApps Session Executor
    principalId: userAssignedIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
  dependsOn: [
    dynamicSessionPool
  ]
}

// Agent Framework Container App
resource containerApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: containerAppName
  location: location
  tags: {
    'azd-service-name': 'agent-framework-app'
  }
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${userAssignedIdentity.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: containerAppsEnvironment.id
    configuration: {
      ingress: {
        external: true
        targetPort: 8080
        corsPolicy: {
          allowedOrigins: ['*']
          allowedMethods: ['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS']
          allowedHeaders: ['*']
        }
      }
      registries: [
        {
          server: containerRegistry.properties.loginServer
          identity: userAssignedIdentity.id
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'agent-framework-app'
          image: 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: [
            {
              name: 'AZURE_OPENAI_ENDPOINT'
              value: openAI.properties.endpoint
            }
            {
              name: 'AZURE_OPENAI_CHAT_DEPLOYMENT_NAME'
              value: openAIModelName
            }
            {
              name: 'AZURE_OPENAI_DEPLOYMENT'
              value: openAIModelName
            }
            {
              name: 'PYTHONUNBUFFERED'
              value: '1'
            }
            {
              name: 'AZURE_CLIENT_ID'
              value: userAssignedIdentity.properties.clientId
            }
            {
              name: 'AZURE_SUBSCRIPTION_ID'
              value: subscription().subscriptionId
            }
            {
              name: 'AZURE_RESOURCE_GROUP'
              value: resourceGroup().name
            }
            {
              name: 'AZURE_SESSION_POOL_NAME'
              value: enableDynamicSessions ? dynamicSessionPool.name : ''
            }
            {
              name: 'AZURE_CONTAINER_APPS_SESSION_POOL_ENDPOINT'
              value: enableDynamicSessions ? dynamicSessionPool.properties.poolManagementEndpoint : ''
            }
            {
              name: 'SESSION_POOL_AUDIENCE'
              value: 'https://dynamicsessions.io/.default'
            }
          ]
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 10
      }
    }
  }
  dependsOn: [
    acrPullRoleAssignment
    openAIUserRoleAssignment
    gptModelDeployment
  ]
}

// Outputs for deployment information
output RESOURCE_GROUP_ID string = resourceGroup().id
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = containerRegistry.properties.loginServer
output AZURE_OPENAI_ENDPOINT string = openAI.properties.endpoint
output AZURE_OPENAI_DEPLOYMENT string = openAIModelName
output CONTAINER_APP_URL string = 'https://${containerApp.properties.configuration.ingress.fqdn}'
output AZURE_OPENAI_RESOURCE_NAME string = openAI.name
output USER_ASSIGNED_IDENTITY_ID string = userAssignedIdentity.id
output DYNAMIC_SESSION_POOL_NAME string = enableDynamicSessions ? dynamicSessionPool.name : ''
output DYNAMIC_SESSION_POOL_ENDPOINT string = enableDynamicSessions ? dynamicSessionPool.properties.poolManagementEndpoint : ''
output DYNAMIC_SESSIONS_ENABLED bool = enableDynamicSessions
output VNET_ENABLED bool = enableVNetIntegration
output VNET_NAME string = enableVNetIntegration ? vnet.name : ''
output VNET_ID string = enableVNetIntegration ? vnet.id : ''
