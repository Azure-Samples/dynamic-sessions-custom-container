# VNet Integration Guide

This guide explains how to deploy the Agent Framework application with VNet integration for enhanced network security and isolation.

## Overview

VNet integration provides:
- **Network Isolation**: Deploy Container Apps in a dedicated subnet
- **Private Communication**: Control inbound/outbound traffic
- **Enhanced Security**: Network-level access controls
- **Custom DNS**: Use private DNS zones
- **Hybrid Connectivity**: Connect to on-premises networks via VPN/ExpressRoute

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  Virtual Network (10.0.0.0/16)                          │
│  ┌───────────────────────────────────────────────────┐  │
│  │  Container Apps Subnet (10.0.0.0/23)              │  │
│  │  Delegated to: Microsoft.App/environments         │  │
│  │                                                    │  │
│  │  ┌──────────────────────────────────────────────┐ │  │
│  │  │  Container Apps Environment                  │ │  │
│  │  │                                              │ │  │
│  │  │  ┌────────────────┐  ┌──────────────────┐  │ │  │
│  │  │  │ Agent App      │  │ Session Pool     │  │ │  │
│  │  │  │ (Main App)     │  │ (Custom Executor)│  │ │  │
│  │  │  └────────────────┘  └──────────────────┘  │ │  │
│  │  │                                              │ │  │
│  │  └──────────────────────────────────────────────┘ │  │
│  │                                                    │  │
│  └───────────────────────────────────────────────────┘  │
│                                                          │
│  Network Security Groups (Optional)                     │
│  Private Endpoints (Optional)                           │
└─────────────────────────────────────────────────────────┘
```

## Deployment

### Enable VNet Integration

Update `infra/main.parameters.json`:

```json
{
  "parameters": {
    "enableVNetIntegration": {
      "value": true
    }
  }
}
```

### Deploy with VNet

```bash
# Deploy with VNet enabled
azd up

# Or provision only
azd provision
```

### Custom VNet Configuration

You can customize the VNet address space in `main.parameters.json`:

```json
{
  "parameters": {
    "enableVNetIntegration": {
      "value": true
    },
    "vnetAddressPrefix": {
      "value": "10.0.0.0/16"
    },
    "containerAppsSubnetPrefix": {
      "value": "10.0.0.0/23"
    }
  }
}
```

## Network Configuration Options

### External vs Internal Environment

By default, the Container Apps Environment is deployed with **external** ingress (public internet access). To make it fully private:

In `infra/main.bicep`, change:
```bicep
vnetConfiguration: enableVNetIntegration ? {
  infrastructureSubnetId: vnet.properties.subnets[0].id
  internal: true  // Change to true for fully private
} : null
```

**External (default)**:
- Apps accessible from internet via public FQDN
- Outbound traffic through VNet
- Suitable for most scenarios

**Internal**:
- Apps only accessible within VNet
- Requires VPN/ExpressRoute or Azure Bastion for access
- Maximum security and isolation

## Session Pool Network Configuration

The session pool is configured with `EgressEnabled` for network isolation:

```bicep
sessionNetworkConfiguration: {
  status: 'EgressEnabled'
}
```

This ensures:
- Session containers can make outbound requests
- Network traffic is routed through the VNet
- Enhanced security for code execution

## Additional Security Considerations

### Network Security Groups (NSGs)

Add NSG rules to control traffic:

```bicep
resource nsg 'Microsoft.Network/networkSecurityGroups@2023-11-01' = {
  name: 'container-apps-nsg'
  location: location
  properties: {
    securityRules: [
      {
        name: 'AllowHTTPS'
        properties: {
          priority: 100
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourcePortRange: '*'
          destinationPortRange: '443'
          sourceAddressPrefix: '*'
          destinationAddressPrefix: '*'
        }
      }
    ]
  }
}
```

### Private Endpoints

Add private endpoints for Azure services:

```bicep
// Private endpoint for Azure OpenAI
resource openAIPrivateEndpoint 'Microsoft.Network/privateEndpoints@2023-11-01' = {
  name: 'pe-openai-${resourceToken}'
  location: location
  properties: {
    subnet: {
      id: vnet.properties.subnets[0].id
    }
    privateLinkServiceConnections: [
      {
        name: 'openai-connection'
        properties: {
          privateLinkServiceId: openAI.id
          groupIds: ['account']
        }
      }
    ]
  }
}
```

## Subnet Requirements

For Container Apps Environment:
- **Minimum size**: /27 (32 addresses)
- **Recommended size**: /23 (512 addresses) for production
- **Delegation**: Must be delegated to `Microsoft.App/environments`
- **Service endpoints**: Optional but recommended for Azure services

## Troubleshooting

### Deployment Fails with Subnet Error

**Issue**: Subnet not large enough or not delegated

**Solution**: 
- Ensure subnet is at least /27
- Verify delegation to `Microsoft.App/environments`
- Check no conflicting delegations exist

### Apps Can't Access Internet

**Issue**: Internal environment without proper routing

**Solution**:
- For external access, set `internal: false`
- For internal with internet, configure NAT Gateway or Azure Firewall

### Session Pool Not Creating

**Issue**: VNet configuration blocking session pool creation

**Solution**:
- Verify workload profiles are enabled
- Check subnet has enough available IP addresses
- Ensure managed identity has proper permissions

## Cost Considerations

VNet integration costs:
- **VNet**: Free
- **Dedicated workload profiles**: Additional charges for E-series profiles
- **NAT Gateway** (if needed): ~$0.045/hour + data processing
- **Private Endpoints** (if used): ~$0.01/hour per endpoint

## Migration from Non-VNet Deployment

To migrate existing deployment to VNet:

1. Set `enableVNetIntegration: true` in parameters
2. Run `azd provision` to update infrastructure
3. Apps will be redeployed in VNet
4. Update any DNS/network rules as needed

**Note**: This will cause brief downtime during migration.

## Best Practices

1. **Subnet Sizing**: Use /23 for production to allow scaling
2. **Network Planning**: Plan IP addressing to avoid conflicts
3. **Security Groups**: Implement least-privilege access rules
4. **Monitoring**: Enable NSG flow logs for traffic analysis
5. **Testing**: Validate connectivity before production deployment
6. **Documentation**: Document custom network rules and configurations

## Verification

After deployment, verify VNet integration:

```bash
# Check VNet exists
az network vnet show --name azvnet<resourceToken> --resource-group <rg-name>

# Check Container Apps Environment VNet config
az containerapp env show --name azcae<resourceToken> --resource-group <rg-name> --query properties.vnetConfiguration

# Test app connectivity
curl https://<app-fqdn>/health
```

## Next Steps

- Configure NSG rules for additional security
- Set up Private DNS zones for internal resolution
- Add private endpoints for Azure services
- Configure custom domains with certificates
- Implement Azure Firewall for advanced security
