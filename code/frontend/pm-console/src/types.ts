export interface PMConsoleStatus {
  code: string;
  explanation: string;
}

export type PMConsoleTargetView = "operator" | "episodes" | "position" | "thesis" | "pm";

export interface PMConsoleTarget {
  target_key: string;
  implemented_views: PMConsoleTargetView[];
  default_view: PMConsoleTargetView;
}

export interface PMConsoleContextResponse {
  generated_at: string;
  workspace_root: string;
  runtime_status: "running" | "not_running" | "unknown";
  runtime_mode: "live" | "replay" | "catchup" | "unknown";
  data_source: "workspace_snapshot" | "runtime_updating_workspace" | "unknown";
  explanation: string;
  thesis_translation?: PMConsoleThesisTranslationAvailability;
}

export interface PMConsoleTargetsResponse {
  generated_at: string;
  targets: PMConsoleTarget[];
  generation?: number;
  reload_error?: string | null;
}

export type PMConsoleOverviewReadiness =
  | "ready"
  | "action_required"
  | "incomplete"
  | "unavailable";

export interface PMConsoleOverviewTarget {
  target_key: string;
  runtime_mode: "live" | "replay" | "catchup" | "unknown";
  workspace_root: string;
  workspace_name: string;
  implemented_views: PMConsoleTargetView[];
  default_view: PMConsoleTargetView;
  readiness: PMConsoleStatus;
  problem_codes: string[];
  current_portfolio_state: PMConsoleCurrentPortfolioState | null;
  latest_pm_decision: PMConsolePMDecisionSummary | null;
  latest_execution: PMConsoleExecutionSummary | null;
  execution_status: PMConsoleStatus;
  latest_thesis_revision: PMConsoleThesisRevisionSummary | null;
  operator_status: PMConsoleStatus;
}

export interface PMConsoleOverviewResponse {
  generated_at: string;
  mounted_target_count: number;
  targets: PMConsoleOverviewTarget[];
  reload_error: string | null;
  generation?: number;
}

export type PMConsoleOperatorStatus = "missing" | "empty" | "ready";

export interface PMConsoleOperatorDocument {
  target_key: string;
  page_path: string;
  content_md: string;
  content_sha256: string;
  status: PMConsoleOperatorStatus;
  editable: boolean;
}

export interface PMConsoleOperatorHistoryVersion {
  target_key: string;
  version_id: string;
  content_sha256: string;
  content_md?: string;
}

export interface PMConsoleOperatorHistoryResponse {
  target_key: string;
  versions: PMConsoleOperatorHistoryVersion[];
}

export type PMConsoleOperatorHistoryVersionResponse = PMConsoleOperatorHistoryVersion;

export interface PMConsoleOperatorConflict {
  error: "stale_operator_context";
  error_message: string;
  current: PMConsoleOperatorDocument;
  current_content_md: string;
  current_content_sha256: string;
}

export interface PMConsoleMarkdownSection {
  page_path: string;
  page_name: string;
  section_name: string;
  content_md: string;
  content_sha256: string | null;
  changed_in_revision: boolean;
}

export interface PMConsoleThesisSectionDiff {
  page_path: string;
  page_name: string;
  section_name: string;
  unified_diff_md: string;
  previous_content_sha256: string;
  content_sha256: string;
  changed: boolean;
}

export interface PMConsoleThesisRevisionSummary {
  revision_id: string;
  target_key: string;
  business_at: string;
  committed_at: string;
  source: "analysis_commit" | "migration_baseline";
  source_event_count: number;
  changed_claim_count: number;
  changed_section_count: number;
  previous_revision_id: string | null;
  is_baseline: boolean;
}

export interface PMConsoleThesisPriceLevel {
  level_id: string;
  display_role: "support" | "resistance" | "watch" | "level";
  role_if_flat: string;
  value: number | null;
  lower: number | null;
  upper: number | null;
  instrument_basis: string;
}

export interface PMConsoleThesisRevisionDetail {
  revision_id: string;
  target_key: string;
  business_at: string;
  committed_at: string;
  source: "analysis_commit" | "migration_baseline";
  analysis_assessment_id: string | null;
  context_packet_id: string | null;
  context_packet_hash: string | null;
  source_event_ids: string[];
  research_memory_write_receipt_ids: string[];
  changed_claim_ids: string[];
  key_claim_ids: string[];
  contested_prior_claim_ids: string[];
  price_level_role_ids: string[];
  price_levels: PMConsoleThesisPriceLevel[];
  previous_revision_id: string | null;
  canonical_bundle_sha256: string;
  is_baseline: boolean;
  sections: PMConsoleMarkdownSection[];
  diffs_from_previous: PMConsoleThesisSectionDiff[];
  linked_pm_decisions: PMConsolePMDecisionSummary[];
}

export interface PMConsoleThesisQuickBriefRequest {
  canonical_bundle_sha256: string;
}

export interface PMConsoleThesisQuickBriefResponse {
  generated_at: string;
  target_key: string;
  revision_id: string;
  canonical_bundle_sha256: string;
  brief_md: string;
  provider: string;
  model: string;
  prompt_version: string;
}

export interface PMConsoleThesisQuickBriefFailure {
  generated_at: string;
  target_key: string;
  revision_id: string;
  canonical_bundle_sha256: string;
  error_code: string;
  error_message: string;
}

export interface PMConsoleThesisRevisionsResponse {
  generated_at: string;
  target_key: string;
  revisions: PMConsoleThesisRevisionSummary[];
  latest_revision: PMConsoleThesisRevisionSummary | null;
}

export interface PMConsoleThesisRevisionDetailResponse {
  generated_at: string;
  target_key: string;
  revision: PMConsoleThesisRevisionDetail;
}

export type PMConsoleThesisTranslationLocale = "zh-CN";

export interface PMConsoleThesisTranslationAvailability {
  locale: PMConsoleThesisTranslationLocale;
  available: boolean;
  reason_code: string | null;
  reason_message: string | null;
}

export interface PMConsoleThesisTranslationRequest {
  canonical_bundle_sha256: string;
  locale: PMConsoleThesisTranslationLocale;
}

export interface PMConsoleThesisTranslationTranslator {
  provider: string;
  model: string;
  prompt_version: string;
}

export interface PMConsoleThesisTranslationVerification {
  passed: boolean;
  failures: string[];
}

export interface PMConsoleTranslatedSection {
  section_id: string;
  source_content_sha256: string;
  page_path: string;
  page_name: string;
  section_name: string;
  translated_content_md: string;
}

export interface PMConsoleThesisTranslationResponse {
  generated_at: string;
  target_key: string;
  revision_id: string;
  locale: PMConsoleThesisTranslationLocale;
  canonical_bundle_sha256: string;
  sections: PMConsoleTranslatedSection[];
  translator: PMConsoleThesisTranslationTranslator;
  verification: PMConsoleThesisTranslationVerification;
}

export interface PMConsoleThesisTranslationFailure {
  generated_at: string;
  target_key: string;
  revision_id: string;
  locale: PMConsoleThesisTranslationLocale;
  canonical_bundle_sha256: string;
  error_code: string;
  error_message: string;
  translator: PMConsoleThesisTranslationTranslator | null;
  verification: PMConsoleThesisTranslationVerification | null;
}

export interface PMConsoleCurrentPortfolioState {
  status: PMConsoleStatus;
  state: string | null;
  target_weight: number | null;
  updated_at: string | null;
  source_pm_decision_id: string | null;
  source_execution_record_id: string | null;
  decision_episode_id: string | null;
}

export interface PMConsoleLine {
  key: "pm_pipeline" | "buy_hold" | "analysis_direct";
  label: string;
  role: "actual" | "baseline" | "analysis";
  status: PMConsoleStatus;
  explanation: string;
}

export interface PMConsolePoint {
  time: string;
  price: number | null;
  pm_pipeline_value: number | null;
  buy_hold_value: number | null;
  analysis_direct_value: number | null;
  pm_target_weight: number | null;
  analysis_direct_target_weight: number | null;
  pm_state: string | null;
  analysis_direct_state: string | null;
}

export interface PMConsoleMarker {
  marker_id: string;
  kind: "pm_decision" | "analysis_assessment";
  lane: "pm" | "analysis";
  label: string;
  time: string;
  business_at: string;
  state: string;
  target_weight: number;
  price: number | null;
  line_value: number | null;
  shape: "circle" | "square" | "arrowUp" | "arrowDown";
  position: "aboveBar" | "belowBar" | "inBar";
  text: string;
  source_id: string;
  pm_decision_id?: string | null;
  execution_record_id?: string | null;
  analysis_assessment_id?: string | null;
}

export interface PMConsoleDecisionSummary {
  decision_id: string;
  business_at: string;
  requested_state: string;
  requested_target_weight: number;
  execution_required: boolean;
  pm_review_request_id: string | null;
}

export interface PMConsoleExecutionSummary {
  execution_record_id: string;
  business_at: string;
  status: string;
  pm_decision_id: string;
  requested_target_weight: number;
  executed_at: string | null;
  target_weight: number | null;
  adjusted_price?: number | null;
  rejection_reason?: string | null;
}

export interface PMConsoleReference {
  reference_id: string | null;
  status: PMConsoleStatus;
  href: string | null;
}

export interface PMConsoleAnalysisAssessment {
  assessment_id: string;
  business_at: string;
  as_if_flat_state: string;
  target_weight: number;
  as_if_flat_rationale_md: string;
  source_event_ids: string[];
  thesis_anchor: PMConsoleReference;
}

export interface PMConsolePMDecisionSummary {
  decision_id: string;
  decision_episode_id: string;
  target_key: string;
  business_at: string;
  decision_available_at: string;
  actual_state_before_decision: string;
  actual_target_weight_before_decision: number;
  requested_state: string;
  requested_target_weight: number;
  execution_required: boolean;
  outcome_status: string;
  pm_review_request_id: string | null;
  review_reasons: string[];
  analysis_conditional_state: string | null;
  analysis_conditional_target_weight: number | null;
}

export interface PMConsolePMDecisionDetail {
  summary: PMConsolePMDecisionSummary;
  review_source: string | null;
  review_reasons: string[];
  rationale_md: string;
  fallback_state_if_clamped: string | null;
  fallback_rationale_md: string | null;
  source_event_ids: string[];
  review_request: PMConsoleReference;
  execution: PMConsoleExecutionSummary | null;
  execution_status: PMConsoleStatus;
  analysis: PMConsoleReference;
  analysis_assessment: PMConsoleAnalysisAssessment | null;
  thesis_revision: PMConsoleReference;
}

export interface PMConsolePMDecisionsResponse {
  generated_at: string;
  target_key: string;
  decisions: PMConsolePMDecisionSummary[];
}

export interface PMConsolePMDecisionDetailResponse {
  generated_at: string;
  target_key: string;
  decision: PMConsolePMDecisionDetail;
}

export interface PMConsoleEpisodeSummary {
  episode_id: string;
  target_key: string;
  phase: "open" | "completed";
  direction: "long" | "short";
  opened_at: string;
  closed_at: string | null;
  close_reason: string | null;
  segment_count: number;
  outcome_status: PMConsoleStatus;
  strategy_return: number | null;
  reflection_status: PMConsoleStatus;
}

export interface PMConsoleEpisodeSegment {
  segment_id: string;
  target_weight: number;
  opened_at: string;
  closed_at: string | null;
  close_reason: string | null;
  strategy_return: number | null;
  outcome_status: PMConsoleStatus;
}

export interface PMConsoleEpisodeTransition {
  state_change_id: string;
  occurred_at: string;
  kind: "entry" | "resize" | "exit";
  state: string;
  previous_target_weight: number | null;
  target_weight: number;
  pm_decision_id: string | null;
  execution_record_id: string | null;
  adjusted_price: number | null;
  raw_price: number | null;
  total_cost_bps: number | null;
  price_basis: string | null;
  rationale_md: string;
}

export interface PMConsoleEpisodeReflection {
  status: PMConsoleStatus;
  obligation_count: number;
  learning_recorded: boolean;
}

export interface PMConsoleEpisodeDetail {
  summary: PMConsoleEpisodeSummary;
  segments: PMConsoleEpisodeSegment[];
  transitions: PMConsoleEpisodeTransition[];
  reflection: PMConsoleEpisodeReflection;
}

export interface PMConsoleEpisodesResponse {
  generated_at: string;
  target_key: string;
  total: number;
  offset: number;
  limit: number;
  episodes: PMConsoleEpisodeSummary[];
}

export interface PMConsoleEpisodeDetailResponse {
  generated_at: string;
  target_key: string;
  episode: PMConsoleEpisodeDetail;
}

export interface PMConsoleAudit {
  source_paths: string[];
  counts: Record<string, number>;
  warnings: string[];
}

export interface PMConsolePositionResponse {
  target_key: string;
  generated_at: string;
  current_portfolio_state: PMConsoleCurrentPortfolioState;
  market_data_status: PMConsoleStatus;
  comparison_status: PMConsoleStatus;
  market_symbol: string | null;
  price_display_decimals: number;
  bar_granularity: string | null;
  market_data_path: string | null;
  base_value: number;
  lines: PMConsoleLine[];
  points: PMConsolePoint[];
  markers: PMConsoleMarker[];
  latest_pm_decision: PMConsoleDecisionSummary | null;
  latest_execution: PMConsoleExecutionSummary | null;
  notes: string[];
  audit: PMConsoleAudit | null;
}
