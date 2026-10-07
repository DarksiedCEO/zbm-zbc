package orchestrator

// How a Revenue Recovery scan is written to the evidence ledger (Revenue
// Recovery fix wave, Oct 6 2026 — sweep findings E-2, E-3, E-8; ADR 0003
// section 13).
//
// A scan is three kinds of POST /ledger/events entry, department
// "revenue_recovery", all with deterministic event ids, so any retry of any
// of them is idempotent (ledger-rust answers 200 to an identical event_id
// and appends nothing):
//
//	rr.<scan_id>.started               rr_scan_started    subject = client_id
//	rr.<scan_id>.f.<finding_id>        rr_finding         subject = finding_id   (one per finding)
//	rr.<scan_id>.completed             rr_scan_completed  subject = client_id
//
// (plus rr.<scan_id>.aborted, rr_scan_aborted, written best-effort when a
// scan fails after it started writing, and — AEGIS L1, Oct 7 2026 — one
//
//	rr.<scan_id>.b.<finding_id>        rr_value_basis     subject = finding_id
//
// right after each finding whose figure is derived from a rate: the base and
// rate it was computed from, which the 280-character finding summary has no
// room for). A scan COUNTS only once its
// completed event exists and agrees with what precedes it: the completed
// event's payload_sha256 is the hash of the scan's manifest (every finding
// event id with its payload hash and summary) and its summary the finding
// count (the manifest also covers every value-basis event), so a
// reader can prove from the ledger alone that it sees exactly the scan's
// findings — no partial scan is ever counted (recorded.go).
//
// The ledger stores a SHA-256 of the full finding JSON (payload_sha256) and a
// 280-character summary. The summary carries what the findings view shows:
//
//	rrf1 a=<agent_id> l=<leak_category> t=<entity_type> e=<entity_id> p=<period|-> v=<amount|-> x=<evidence_class> k=<classification|-> n=<confidence|-> m=<methodology_id>
//
// The client is the scan's (started event subject) and the finding id the
// event's subject; both are re-derived and checked on read. Every value's
// charset excludes space and "=", and none can be "-" (first character
// alphanumeric), so the encoding is unambiguous. Worst case 271 characters
// (ledger_record_test.go; 272 before AEGIS M2, when ESTIMATED could carry
// financially_verified/very_high): the field bounds in detection-py
// zbm_schema/limits.py are what make it fit.

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"math/big"
	"regexp"
	"sort"
	"strconv"
	"strings"

	"github.com/DarksiedCEO/zbm-zbc/services/orchestrator-go/internal/client"
)

const (
	ledgerDepartment = "revenue_recovery"
	ledgerActor      = "orchestrator_go"

	eventTypeScanStarted   = "rr_scan_started"
	eventTypeFinding       = "rr_finding"
	eventTypeScanCompleted = "rr_scan_completed"
	eventTypeScanAborted   = "rr_scan_aborted"
	eventTypeValueBasis    = "rr_value_basis"

	maxSummaryChars = 280 // ledger-rust EventInput rule

	findingSummaryVersion = "rrf1"
	startedSummaryVersion = "rrs1"
	doneSummaryVersion    = "rrc1"
	abortSummaryVersion   = "rra1"
	basisSummaryVersion   = "rrb1"

	// Field bounds — detection-py zbm_schema/limits.py.
	maxIDChars            = 64
	maxAgentIDChars       = 32
	maxLeakCategoryChars  = 33 // longest LeakCategory value: cross_channel_misattribution_risk
	maxPeriodChars        = 16
	maxMethodologyIDChars = 24
	maxClassificationLen  = 20 // financially_verified
	maxConfidenceLen      = 9  // very_high
	maxRatePercentChars   = 34 // zbm_schema limits.RATE_PERCENT_MAX_CHARS
)

var (
	idRE       = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._:/@+#-]*$`)
	clientIDRE = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._:-]*$`)
	slugRE     = regexp.MustCompile(`^[a-z0-9][a-z0-9_]*$`)
	agentIDRE  = regexp.MustCompile(`^[a-z0-9][a-z0-9-]*$`)
	findingRE  = regexp.MustCompile(`^rrf1-[0-9a-f]{40}$`)
	scanIDRE   = regexp.MustCompile(`^[0-9a-f]{32}$`)
	rateRE     = regexp.MustCompile(`^(0|[1-9][0-9]{0,2})(\.[0-9]+)?$`)

	entityTypes    = map[string]bool{"order": true, "subscription": true, "contract_term": true, "platform": true}
	evidenceValues = map[string]bool{"OBSERVED": true, "ESTIMATED": true, "MODELED": true, "UNKNOWN": true}
)

func matches(re *regexp.Regexp, s string, maxLen int) bool {
	return len(s) >= 1 && len(s) <= maxLen && re.MatchString(s)
}

// ValidClientID: a tenant id is the subject_id of every scan event, so it
// must satisfy ledger-rust's subject charset ([A-Za-z0-9._:-], here at most
// 64 characters and starting alphanumeric — zbm_schema ClientId).
func ValidClientID(s string) bool { return matches(clientIDRE, s, maxIDChars) }

func sha256Hex(b []byte) string {
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

func scanEventID(scanID, suffix string) string { return "rr." + scanID + "." + suffix }

func findingEventID(scanID, findingID string) string { return "rr." + scanID + ".f." + findingID }

func basisEventID(scanID, findingID string) string { return "rr." + scanID + ".b." + findingID }

// AEGIS M2 (Oct 7 2026): a value's labels can never claim more than its
// evidence class supports — the same rule as detection-py
// zbm_schema.labels_exceed_evidence. Only OBSERVED evidence (exact
// arithmetic on recorded transactions/terms) supports the "observed" or
// "financially_verified" classification or a "high"/"very_high" confidence.
var (
	observationClassifications = map[string]bool{"observed": true, "financially_verified": true}
	highConfidences            = map[string]bool{"high": true, "very_high": true}
)

// labelsExceedEvidence says why the labels overclaim, or "" if they do not.
func labelsExceedEvidence(evidence, classification, confidence string) string {
	if evidence == "OBSERVED" || evidence == "UNKNOWN" {
		return ""
	}
	if observationClassifications[classification] {
		return fmt.Sprintf("classification %s needs OBSERVED evidence, the figure is %s", classification, evidence)
	}
	if highConfidences[confidence] {
		return fmt.Sprintf("confidence %s needs OBSERVED evidence, the figure is %s", confidence, evidence)
	}
	return ""
}

// validRate: a rate in percent as detection-py writes it — a plain decimal,
// 0 < rate <= 100, at most maxRatePercentChars characters.
func validRate(s string) bool {
	if !matches(rateRE, s, maxRatePercentChars) {
		return false
	}
	r, ok := new(big.Rat).SetString(s)
	return ok && r.Sign() > 0 && r.Cmp(big.NewRat(100, 1)) <= 0
}

// valueBasisAmount is base x rate / 100, rounded half-up to cents, as
// canonical money text — exactly detection-py's percent_of (exact Decimal
// arithmetic, then one half-up quantize). big.Rat is exact, so the two can
// never disagree by a rounding step. Inputs must already be valid.
func valueBasisAmount(base client.Money, rate string) (string, bool) {
	b, ok1 := new(big.Rat).SetString(base.String())
	r, ok2 := new(big.Rat).SetString(rate)
	if !ok1 || !ok2 {
		return "", false
	}
	cents := new(big.Rat).Mul(b, r) // dollars x percent = cents
	cents.Add(cents, big.NewRat(1, 2))
	q := new(big.Int).Quo(cents.Num(), cents.Denom()) // floor: the value is positive
	if q.Sign() <= 0 {
		return "", false
	}
	str := fmt.Sprintf("%03s", q.String())
	return str[:len(str)-2] + "." + str[len(str)-2:], true
}

// checkValueBasis: a value basis needs a value, valid inputs, and must
// reproduce the finding's amount exactly.
func checkValueBasis(vb *client.ValueBasis, amount *client.Money) error {
	if amount == nil {
		return fmt.Errorf("value_basis without a recoverable_value")
	}
	if !vb.BaseUSD.IsPositive() || !validRate(vb.RatePercent) {
		return fmt.Errorf("value_basis is out of contract")
	}
	if got, ok := valueBasisAmount(vb.BaseUSD, vb.RatePercent); !ok || got != amount.String() {
		return fmt.Errorf("value_basis %s x %s%% does not reproduce the amount %s", vb.BaseUSD.String(), vb.RatePercent, amount.String())
	}
	return nil
}

// checkFinding verifies everything the orchestrator relies on before a
// finding is counted, correlated or recorded: it belongs to the scanned
// tenant, it came from the agent that was called, its id is the derived
// hash, its evidence class agrees with its value, and every field fits the
// ledger summary. Any failure fails the scan — nothing is truncated.
func checkFinding(f client.Finding, tenant, wantAgent string) error {
	if f.ClientID != tenant {
		return fmt.Errorf("finding %.80q is for client %.80q, not the scanned client %q", f.FindingID, f.ClientID, tenant)
	}
	if f.AgentID != wantAgent {
		return fmt.Errorf("finding %.80q claims agent %.80q but came from %s", f.FindingID, f.AgentID, wantAgent)
	}
	period := ""
	if f.PeriodLabel != nil {
		period = *f.PeriodLabel
		if !matches(idRE, period, maxPeriodChars) {
			return fmt.Errorf("finding %.80q has an invalid period_label", f.FindingID)
		}
	}
	switch {
	case !matches(agentIDRE, f.AgentID, maxAgentIDChars):
		return fmt.Errorf("finding %.80q has an invalid agent_id", f.FindingID)
	case !matches(slugRE, f.LeakCategory, maxLeakCategoryChars):
		return fmt.Errorf("finding %.80q has an invalid leak_category", f.FindingID)
	case !entityTypes[f.EntityType]:
		return fmt.Errorf("finding %.80q has an unknown entity_type %.40q", f.FindingID, f.EntityType)
	case !matches(idRE, f.EntityID, maxIDChars):
		return fmt.Errorf("finding %.80q has an invalid entity_id", f.FindingID)
	case !matches(slugRE, f.MethodologyID, maxMethodologyIDChars):
		return fmt.Errorf("finding %.80q has an invalid methodology_id", f.FindingID)
	case f.Methodology == "":
		return fmt.Errorf("finding %.80q has no methodology note", f.FindingID)
	case !evidenceValues[f.EvidenceClass]:
		return fmt.Errorf("finding %.80q has an unknown evidence_class %.20q", f.FindingID, f.EvidenceClass)
	case (f.RecoverableValue == nil) != (f.EvidenceClass == "UNKNOWN"):
		return fmt.Errorf("finding %.80q: evidence_class %s disagrees with its recoverable_value", f.FindingID, f.EvidenceClass)
	}
	if rv := f.RecoverableValue; rv != nil {
		if !matches(slugRE, rv.Classification, maxClassificationLen) || !matches(slugRE, rv.Confidence, maxConfidenceLen) {
			return fmt.Errorf("finding %.80q has an invalid value label", f.FindingID)
		}
		if why := labelsExceedEvidence(f.EvidenceClass, rv.Classification, rv.Confidence); why != "" {
			return fmt.Errorf("finding %.80q: its labels claim more than its evidence supports (%s)", f.FindingID, why)
		}
	}
	if f.ValueBasis != nil {
		var amount *client.Money
		if f.RecoverableValue != nil {
			amount = &f.RecoverableValue.AmountUSD
		}
		if err := checkValueBasis(f.ValueBasis, amount); err != nil {
			return fmt.Errorf("finding %.80q: %v", f.FindingID, err)
		}
	}
	if want := ComputeFindingID(f.ClientID, f.AgentID, f.EntityType, f.EntityID, period); f.FindingID != want {
		return fmt.Errorf("finding id %.80q is not the derived id %s", f.FindingID, want)
	}
	return nil
}

func dashIfEmpty(s string) string {
	if s == "" {
		return "-"
	}
	return s
}

// encodeFindingSummary is the rrf1 summary of a finding checkFinding passed.
func encodeFindingSummary(f client.Finding) (string, error) {
	period, amount, class, conf := "", "", "", ""
	if f.PeriodLabel != nil {
		period = *f.PeriodLabel
	}
	if rv := f.RecoverableValue; rv != nil {
		amount, class, conf = rv.AmountUSD.String(), rv.Classification, rv.Confidence
	}
	s := strings.Join([]string{
		findingSummaryVersion,
		"a=" + f.AgentID,
		"l=" + f.LeakCategory,
		"t=" + f.EntityType,
		"e=" + f.EntityID,
		"p=" + dashIfEmpty(period),
		"v=" + dashIfEmpty(amount),
		"x=" + f.EvidenceClass,
		"k=" + dashIfEmpty(class),
		"n=" + dashIfEmpty(conf),
		"m=" + f.MethodologyID,
	}, " ")
	if len(s) > maxSummaryChars {
		return "", fmt.Errorf("finding %s: ledger summary is %d characters (max %d)", f.FindingID, len(s), maxSummaryChars)
	}
	return s, nil
}

// recordedFields is a decoded rrf1 summary.
type recordedFields struct {
	AgentID, LeakCategory, EntityType, EntityID, EvidenceClass, MethodologyID string
	PeriodLabel                                                               *string
	AmountUSD                                                                 *client.Money
	ValueClassification, DecisionConfidence                                   *string
}

func optional(v string) *string {
	if v == "-" {
		return nil
	}
	return &v
}

// decodeFindingSummary parses an rrf1 summary strictly: exact keys in exact
// order, every value in its charset, amount canonical money, and the
// evidence class consistent with the amount. Anything else is an error.
func decodeFindingSummary(s string) (*recordedFields, error) {
	keys := []string{"a", "l", "t", "e", "p", "v", "x", "k", "n", "m"}
	parts := strings.Split(s, " ")
	if len(parts) != len(keys)+1 || parts[0] != findingSummaryVersion {
		return nil, fmt.Errorf("not an %s summary", findingSummaryVersion)
	}
	v := map[string]string{}
	for i, k := range keys {
		val, ok := strings.CutPrefix(parts[i+1], k+"=")
		if !ok || val == "" {
			return nil, fmt.Errorf("summary field %d is not %s=", i+1, k)
		}
		v[k] = val
	}
	r := &recordedFields{
		AgentID: v["a"], LeakCategory: v["l"], EntityType: v["t"], EntityID: v["e"],
		EvidenceClass: v["x"], MethodologyID: v["m"],
		PeriodLabel: optional(v["p"]), ValueClassification: optional(v["k"]), DecisionConfidence: optional(v["n"]),
	}
	switch {
	case !matches(agentIDRE, r.AgentID, maxAgentIDChars),
		!matches(slugRE, r.LeakCategory, maxLeakCategoryChars),
		!entityTypes[r.EntityType],
		!matches(idRE, r.EntityID, maxIDChars),
		!evidenceValues[r.EvidenceClass],
		!matches(slugRE, r.MethodologyID, maxMethodologyIDChars):
		return nil, fmt.Errorf("summary field out of contract")
	}
	if r.PeriodLabel != nil && !matches(idRE, *r.PeriodLabel, maxPeriodChars) {
		return nil, fmt.Errorf("summary period out of contract")
	}
	if a := v["v"]; a != "-" {
		m, err := client.ParseMoney(a)
		if err != nil || !m.IsPositive() {
			return nil, fmt.Errorf("summary amount is not positive canonical money")
		}
		r.AmountUSD = &m
	}
	hasValue := r.AmountUSD != nil
	if hasValue != (r.ValueClassification != nil) || hasValue != (r.DecisionConfidence != nil) ||
		hasValue == (r.EvidenceClass == "UNKNOWN") {
		return nil, fmt.Errorf("summary amount, labels and evidence class disagree")
	}
	if hasValue && (!matches(slugRE, *r.ValueClassification, maxClassificationLen) || !matches(slugRE, *r.DecisionConfidence, maxConfidenceLen)) {
		return nil, fmt.Errorf("summary value labels out of contract")
	}
	return r, nil
}

// findingEvent is the ledger event for one finding of scan scanID.
func findingEvent(scanID string, f client.Finding) (client.EventInput, error) {
	summary, err := encodeFindingSummary(f)
	if err != nil {
		return client.EventInput{}, err
	}
	payload, err := json.Marshal(f)
	if err != nil {
		return client.EventInput{}, fmt.Errorf("marshal finding %s: %w", f.FindingID, err)
	}
	return client.EventInput{
		EventID:       findingEventID(scanID, f.FindingID),
		Department:    ledgerDepartment,
		EventType:     eventTypeFinding,
		Actor:         ledgerActor,
		SubjectID:     f.FindingID,
		PayloadSHA256: sha256Hex(payload),
		Summary:       summary,
	}, nil
}

// basisEvent is the rr_value_basis event of a finding checkFinding passed
// that carries a value basis (L1). Its summary is
//
//	rrb1 base=<money> rate=<rate percent>
//
// at most 5+1+23+1+39 = 69 characters.
func basisEvent(scanID string, f client.Finding) (client.EventInput, error) {
	vb := f.ValueBasis
	payload, err := json.Marshal(vb)
	if err != nil {
		return client.EventInput{}, fmt.Errorf("marshal value basis of %s: %w", f.FindingID, err)
	}
	return client.EventInput{
		EventID:       basisEventID(scanID, f.FindingID),
		Department:    ledgerDepartment,
		EventType:     eventTypeValueBasis,
		Actor:         ledgerActor,
		SubjectID:     f.FindingID,
		PayloadSHA256: sha256Hex(payload),
		Summary:       basisSummaryVersion + " base=" + vb.BaseUSD.String() + " rate=" + vb.RatePercent,
	}, nil
}

// decodeBasisSummary parses an rrb1 summary strictly.
func decodeBasisSummary(s string) (*client.ValueBasis, error) {
	parts := strings.Split(s, " ")
	if len(parts) != 3 || parts[0] != basisSummaryVersion {
		return nil, fmt.Errorf("not an %s summary", basisSummaryVersion)
	}
	base, ok1 := strings.CutPrefix(parts[1], "base=")
	rate, ok2 := strings.CutPrefix(parts[2], "rate=")
	if !ok1 || !ok2 {
		return nil, fmt.Errorf("value basis summary fields are not base= rate=")
	}
	m, err := client.ParseMoney(base)
	if err != nil || !m.IsPositive() || !validRate(rate) {
		return nil, fmt.Errorf("value basis summary out of contract")
	}
	return &client.ValueBasis{BaseUSD: m, RatePercent: rate}, nil
}

// startedPayload is hashed into the started event; its summary repeats the
// parts a reader needs.
type startedPayload struct {
	ScanID          string   `json:"scan_id"`
	ClientID        string   `json:"client_id"`
	AsOf            string   `json:"as_of"`
	DataSource      string   `json:"data_source"`
	Fixture         bool     `json:"fixture"`
	TenantDefaulted bool     `json:"tenant_defaulted"`
	Agents          []string `json:"agents"`
}

func boolDigit(b bool) string {
	if b {
		return "1"
	}
	return "0"
}

func startedEvent(p startedPayload) client.EventInput {
	payload, _ := json.Marshal(p) // strings and bools only: cannot fail
	return client.EventInput{
		EventID:       scanEventID(p.ScanID, "started"),
		Department:    ledgerDepartment,
		EventType:     eventTypeScanStarted,
		Actor:         ledgerActor,
		SubjectID:     p.ClientID,
		PayloadSHA256: sha256Hex(payload),
		Summary: fmt.Sprintf("%s src=%s fixture=%s defaulted=%s asof=%s agents=%d", startedSummaryVersion,
			p.DataSource, boolDigit(p.Fixture), boolDigit(p.TenantDefaulted), p.AsOf, len(p.Agents)),
	}
}

type startedInfo struct {
	DataSource      string
	Fixture         bool
	TenantDefaulted bool
	AsOf            string
}

func decodeStartedSummary(s string) (*startedInfo, error) {
	parts := strings.Split(s, " ")
	if len(parts) != 6 || parts[0] != startedSummaryVersion {
		return nil, fmt.Errorf("not an %s summary", startedSummaryVersion)
	}
	get := func(i int, k string) (string, error) {
		v, ok := strings.CutPrefix(parts[i], k+"=")
		if !ok || v == "" {
			return "", fmt.Errorf("started summary field %d is not %s=", i, k)
		}
		return v, nil
	}
	var info startedInfo
	var err error
	var fx, df string
	if info.DataSource, err = get(1, "src"); err != nil {
		return nil, err
	}
	if fx, err = get(2, "fixture"); err != nil {
		return nil, err
	}
	if df, err = get(3, "defaulted"); err != nil {
		return nil, err
	}
	if info.AsOf, err = get(4, "asof"); err != nil {
		return nil, err
	}
	if _, err = get(5, "agents"); err != nil {
		return nil, err
	}
	info.Fixture, info.TenantDefaulted = fx == "1", df == "1"
	return &info, nil
}

// manifestHash is the payload_sha256 of a scan's completed event: the hash
// of the scan id, the client and, for every finding event and every value
// basis event (L1; a scan recorded before Oct 7 2026 has none, so its hash is
// unchanged), its event id, payload hash and summary, sorted by event id. The reader recomputes it
// from the finding events it actually finds for the scan, so the completion
// record commits to every recorded amount and label, not only to the
// (unstored) payloads.
func manifestHash(scanID, clientID string, events [][3]string) string {
	sorted := append([][3]string(nil), events...)
	sort.Slice(sorted, func(i, j int) bool { return sorted[i][0] < sorted[j][0] })
	b, _ := json.Marshal(struct {
		ScanID   string      `json:"scan_id"`
		ClientID string      `json:"client_id"`
		Findings [][3]string `json:"findings"`
	}{scanID, clientID, append([][3]string{}, sorted...)})
	return sha256Hex(b)
}

// completedEvent: n is the scan's finding count; events is the manifest
// (finding and value-basis events).
func completedEvent(scanID, clientID string, n int, events [][3]string) client.EventInput {
	return client.EventInput{
		EventID:       scanEventID(scanID, "completed"),
		Department:    ledgerDepartment,
		EventType:     eventTypeScanCompleted,
		Actor:         ledgerActor,
		SubjectID:     clientID,
		PayloadSHA256: manifestHash(scanID, clientID, events),
		Summary:       doneSummaryVersion + " n=" + strconv.Itoa(n),
	}
}

func decodeCompletedSummary(s string) (int, error) {
	rest, ok := strings.CutPrefix(s, doneSummaryVersion+" n=")
	if !ok {
		return 0, fmt.Errorf("not an %s summary", doneSummaryVersion)
	}
	n, err := strconv.Atoi(rest)
	if err != nil || n < 0 || strconv.Itoa(n) != rest {
		return 0, fmt.Errorf("completed summary count is not a number")
	}
	return n, nil
}

// abortedEvent marks a scan that failed after it began writing. Informative
// only: a scan without a completed event is never counted, aborted or not.
func abortedEvent(scanID, clientID, reason string) client.EventInput {
	clean := strings.Map(func(r rune) rune {
		if r < 0x20 || r == 0x7f || (r >= 0x80 && r <= 0x9f) {
			return ' '
		}
		return r
	}, reason)
	if n := len([]rune(clean)); n > 200 {
		clean = string([]rune(clean)[:200])
	}
	return client.EventInput{
		EventID:       scanEventID(scanID, "aborted"),
		Department:    ledgerDepartment,
		EventType:     eventTypeScanAborted,
		Actor:         ledgerActor,
		SubjectID:     clientID,
		PayloadSHA256: sha256Hex([]byte(reason)),
		Summary:       abortSummaryVersion + " " + clean,
	}
}
