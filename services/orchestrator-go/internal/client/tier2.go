package client

import "context"
import "net/http"

// Tier 2 request/fixture shapes. Kept loosely typed (map[string]any) like
// Order/Subscription above — the orchestrator routes data, it does not
// re-validate Tier 2 business rules (that's detection-py's job).
type ServerSideEvent map[string]any
type ChannelTouchpoint map[string]any
type PlatformConnectionStatus map[string]any
type ContractTerm map[string]any

type serverSideEventsRequest struct {
	ClientID string            `json:"client_id"`
	Events   []ServerSideEvent `json:"events"`
}
type channelTouchpointsRequest struct {
	ClientID    string              `json:"client_id"`
	Touchpoints []ChannelTouchpoint `json:"touchpoints"`
}
type platformConnectionsRequest struct {
	ClientID string                     `json:"client_id"`
	Statuses []PlatformConnectionStatus `json:"statuses"`
}
type contractTermsRequest struct {
	ClientID string         `json:"client_id"`
	Terms    []ContractTerm `json:"terms"`
}

func (c *DetectionClient) FixtureServerSideEvents(ctx context.Context) ([]ServerSideEvent, error) {
	var out []ServerSideEvent
	err := c.doJSON(ctx, http.MethodGet, "/fixtures/tier2/server-side-events", nil, &out)
	return out, err
}

func (c *DetectionClient) FixtureChannelTouchpoints(ctx context.Context) ([]ChannelTouchpoint, error) {
	var out []ChannelTouchpoint
	err := c.doJSON(ctx, http.MethodGet, "/fixtures/tier2/channel-touchpoints", nil, &out)
	return out, err
}

func (c *DetectionClient) FixturePlatformConnections(ctx context.Context) ([]PlatformConnectionStatus, error) {
	var out []PlatformConnectionStatus
	err := c.doJSON(ctx, http.MethodGet, "/fixtures/tier2/platform-connections", nil, &out)
	return out, err
}

func (c *DetectionClient) FixtureContractTerms(ctx context.Context) ([]ContractTerm, error) {
	var out []ContractTerm
	err := c.doJSON(ctx, http.MethodGet, "/fixtures/tier2/contract-terms", nil, &out)
	return out, err
}

func (c *DetectionClient) DetectServerSideAttribution(ctx context.Context, clientID string, events []ServerSideEvent) ([]Finding, error) {
	var out findingsResponse
	err := c.doJSON(ctx, http.MethodPost, "/agents/server-side-attribution/detect", serverSideEventsRequest{ClientID: clientID, Events: nonNil(events)}, &out)
	return out.Findings, err
}

func (c *DetectionClient) DetectCrossChannelAttribution(ctx context.Context, clientID string, tps []ChannelTouchpoint) ([]Finding, error) {
	var out findingsResponse
	err := c.doJSON(ctx, http.MethodPost, "/agents/cross-channel-attribution/detect", channelTouchpointsRequest{ClientID: clientID, Touchpoints: nonNil(tps)}, &out)
	return out.Findings, err
}

func (c *DetectionClient) DetectPlatformIntegration(ctx context.Context, clientID string, statuses []PlatformConnectionStatus) ([]Finding, error) {
	var out findingsResponse
	err := c.doJSON(ctx, http.MethodPost, "/agents/platform-integration/detect", platformConnectionsRequest{ClientID: clientID, Statuses: nonNil(statuses)}, &out)
	return out.Findings, err
}

func (c *DetectionClient) DetectContractPricingTermDrift(ctx context.Context, clientID string, terms []ContractTerm) ([]Finding, error) {
	var out findingsResponse
	err := c.doJSON(ctx, http.MethodPost, "/agents/contract-pricing-term-drift/detect", contractTermsRequest{ClientID: clientID, Terms: nonNil(terms)}, &out)
	return out.Findings, err
}
