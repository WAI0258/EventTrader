import type {
  PMConsoleContextResponse,
  PMConsoleEpisodeDetailResponse,
  PMConsoleEpisodesResponse,
  PMConsoleOperatorConflict,
  PMConsoleOperatorDocument,
  PMConsoleOperatorHistoryResponse,
  PMConsoleOperatorHistoryVersionResponse,
  PMConsolePositionResponse,
  PMConsoleAnalysisAssessment,
  PMConsolePMDecisionDetailResponse,
  PMConsolePMDecisionsResponse,
  PMConsoleThesisTranslationFailure,
  PMConsoleThesisTranslationRequest,
  PMConsoleThesisTranslationResponse,
  PMConsoleThesisRevisionDetailResponse,
  PMConsoleThesisQuickBriefFailure,
  PMConsoleThesisQuickBriefRequest,
  PMConsoleThesisQuickBriefResponse,
  PMConsoleThesisRevisionsResponse,
  PMConsoleTargetsResponse,
  PMConsoleOverviewResponse,
} from "./types";

const API_BASE_URL =
  import.meta.env.VITE_PM_CONSOLE_API_BASE_URL ?? "http://127.0.0.1:8765";

export class PMConsoleApiError<TPayload = unknown> extends Error {
  readonly status: number;
  readonly payload: TPayload | null;

  constructor(message: string, status: number, payload: TPayload | null = null) {
    super(message);
    this.name = "PMConsoleApiError";
    this.status = status;
    this.payload = payload;
  }
}

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `Request failed with status ${response.status}`);
  }
  return (await response.json()) as T;
}

async function parseJsonErrorPayload<T>(response: Response): Promise<T | null> {
  const contentType = response.headers.get("Content-Type") ?? "";
  if (!contentType.includes("application/json")) {
    return null;
  }
  try {
    return (await response.json()) as T;
  } catch {
    return null;
  }
}

export function fetchTargets(): Promise<PMConsoleTargetsResponse> {
  return getJson<PMConsoleTargetsResponse>("/api/targets");
}

export function fetchOverview(): Promise<PMConsoleOverviewResponse> {
  return getJson<PMConsoleOverviewResponse>("/api/overview");
}

export function fetchContext(
  target?: string
): Promise<PMConsoleContextResponse> {
  const path = target
    ? `/api/targets/${encodeURIComponent(target)}/context`
    : "/api/context";
  return getJson<PMConsoleContextResponse>(path);
}

export function fetchTargetPosition(
  target: string
): Promise<PMConsolePositionResponse> {
  return getJson<PMConsolePositionResponse>(
    `/api/targets/${encodeURIComponent(target)}/position`
  );
}

export function fetchTargetEpisodes(
  target: string,
  options: {
    offset?: number;
    limit?: number;
    phase?: string | null;
    query?: string | null;
    anchorEpisodeId?: string | null;
  } = {},
): Promise<PMConsoleEpisodesResponse> {
  const params = new URLSearchParams();
  if (options.offset) params.set("offset", String(options.offset));
  if (options.limit) params.set("limit", String(options.limit));
  if (options.phase) params.set("phase", options.phase);
  if (options.query) params.set("q", options.query);
  if (options.anchorEpisodeId) params.set("anchor_episode_id", options.anchorEpisodeId);
  const suffix = params.size ? `?${params.toString()}` : "";
  return getJson<PMConsoleEpisodesResponse>(
    `/api/targets/${encodeURIComponent(target)}/episodes${suffix}`,
  );
}

export function fetchTargetEpisodeDetail(
  target: string,
  episodeId: string,
): Promise<PMConsoleEpisodeDetailResponse> {
  return getJson<PMConsoleEpisodeDetailResponse>(
    `/api/targets/${encodeURIComponent(target)}/episodes/${encodeURIComponent(episodeId)}`,
  );
}

export function fetchTargetOperator(target: string): Promise<PMConsoleOperatorDocument> {
  return getJson<PMConsoleOperatorDocument>(
    `/api/targets/${encodeURIComponent(target)}/operator`,
  );
}

export function fetchTargetOperatorHistory(
  target: string,
): Promise<PMConsoleOperatorHistoryResponse> {
  return getJson<PMConsoleOperatorHistoryResponse>(
    `/api/targets/${encodeURIComponent(target)}/operator/history`,
  );
}

export function fetchTargetOperatorHistoryVersion(
  target: string,
  versionId: string,
): Promise<PMConsoleOperatorHistoryVersionResponse> {
  return getJson<PMConsoleOperatorHistoryVersionResponse>(
    `/api/targets/${encodeURIComponent(target)}/operator/history/${encodeURIComponent(versionId)}`,
  );
}

export async function updateTargetOperator(
  target: string,
  contentMd: string,
  expectedContentSha256: string,
): Promise<PMConsoleOperatorDocument> {
  return writeOperatorRequest(
    `/api/targets/${encodeURIComponent(target)}/operator`,
    "PUT",
    { content_md: contentMd, expected_content_sha256: expectedContentSha256 },
  );
}

export async function restoreTargetOperatorHistoryVersion(
  target: string,
  versionId: string,
  expectedContentSha256: string,
): Promise<PMConsoleOperatorDocument> {
  return writeOperatorRequest(
    `/api/targets/${encodeURIComponent(target)}/operator/history/${encodeURIComponent(versionId)}/restore`,
    "POST",
    { expected_content_sha256: expectedContentSha256 },
  );
}

async function writeOperatorRequest<T>(
  path: string,
  method: "PUT" | "POST",
  body: Record<string, string>,
): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    method,
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const payload = await parseJsonErrorPayload<PMConsoleOperatorConflict>(response);
    throw new PMConsoleApiError(
      payload?.error_message ?? `Request failed with status ${response.status}`,
      response.status,
      payload,
    );
  }
  return (await response.json()) as T;
}

export function fetchTargetThesisRevisions(
  target: string
): Promise<PMConsoleThesisRevisionsResponse> {
  return getJson<PMConsoleThesisRevisionsResponse>(
    `/api/targets/${encodeURIComponent(target)}/thesis-revisions`
  );
}

export function fetchTargetThesisRevisionDetail(
  target: string,
  revisionId: string
): Promise<PMConsoleThesisRevisionDetailResponse> {
  return getJson<PMConsoleThesisRevisionDetailResponse>(
    `/api/targets/${encodeURIComponent(target)}/thesis-revisions/${encodeURIComponent(revisionId)}`
  ).then((payload) => ({
    ...payload,
    revision: {
      ...payload.revision,
      price_levels: payload.revision.price_levels ?? [],
    },
  }));
}

export function fetchTargetPmDecisions(
  target: string
): Promise<PMConsolePMDecisionsResponse> {
  return getJson<PMConsolePMDecisionsResponse>(
    `/api/targets/${encodeURIComponent(target)}/pm-decisions`
  );
}

export function fetchTargetPmDecisionDetail(
  target: string,
  decisionId: string
): Promise<PMConsolePMDecisionDetailResponse> {
  return getJson<PMConsolePMDecisionDetailResponse>(
    `/api/targets/${encodeURIComponent(target)}/pm-decisions/${encodeURIComponent(decisionId)}`
  );
}

export function fetchTargetAnalysisAssessment(
  target: string,
  assessmentId: string
): Promise<PMConsoleAnalysisAssessment> {
  return getJson<PMConsoleAnalysisAssessment>(
    `/api/targets/${encodeURIComponent(target)}/analysis-assessments/${encodeURIComponent(assessmentId)}`
  );
}

export async function translateTargetThesisRevision(
  target: string,
  revisionId: string,
  request: PMConsoleThesisTranslationRequest
): Promise<PMConsoleThesisTranslationResponse> {
  const response = await fetch(
    `${API_BASE_URL}/api/targets/${encodeURIComponent(target)}/thesis-revisions/${encodeURIComponent(revisionId)}/translate`,
    {
      method: "POST",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
      },
      body: JSON.stringify(request),
    }
  );
  if (!response.ok) {
    const payload =
      await parseJsonErrorPayload<PMConsoleThesisTranslationFailure>(response);
    const fallbackMessage = `Request failed with status ${response.status}`;
    throw new PMConsoleApiError(
      payload?.error_message ?? fallbackMessage,
      response.status,
      payload
    );
  }
  return (await response.json()) as PMConsoleThesisTranslationResponse;
}

export async function buildTargetThesisQuickBrief(
  target: string,
  revisionId: string,
  request: PMConsoleThesisQuickBriefRequest
): Promise<PMConsoleThesisQuickBriefResponse> {
  const response = await fetch(
    `${API_BASE_URL}/api/targets/${encodeURIComponent(target)}/thesis-revisions/${encodeURIComponent(revisionId)}/brief`,
    {
      method: "POST",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify(request),
    }
  );
  if (!response.ok) {
    const payload = await parseJsonErrorPayload<PMConsoleThesisQuickBriefFailure>(response);
    throw new PMConsoleApiError(
      payload?.error_message ?? `Request failed with status ${response.status}`,
      response.status,
      payload
    );
  }
  return (await response.json()) as PMConsoleThesisQuickBriefResponse;
}
