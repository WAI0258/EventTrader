import { useMutation, useQuery } from "@tanstack/react-query";
import {
  fetchContext,
  fetchTargetPosition,
  fetchTargetEpisodeDetail,
  fetchTargetEpisodes,
  fetchTargetPmDecisionDetail,
  fetchTargetPmDecisions,
  fetchTargetAnalysisAssessment,
  translateTargetThesisRevision,
  buildTargetThesisQuickBrief,
  fetchTargetThesisRevisionDetail,
  fetchTargetThesisRevisions,
  fetchOverview,
  fetchTargetOperator,
  fetchTargetOperatorHistory,
  fetchTargetOperatorHistoryVersion,
  restoreTargetOperatorHistoryVersion,
  updateTargetOperator,
  fetchTargets,
} from "../../api";
import type { PMConsoleThesisTranslationRequest } from "../../types";

export function usePmConsoleContextQuery(targetKey?: string | null) {
  return useQuery({
    queryKey: ["context", targetKey ?? null],
    queryFn: () => fetchContext(targetKey ?? undefined),
    enabled: targetKey !== null,
  });
}

export function usePmConsoleTargetsQuery() {
  return useQuery({
    queryKey: ["targets"],
    queryFn: fetchTargets,
    staleTime: 5_000,
    refetchInterval: 7_000,
  });
}

export function usePmConsoleOverviewQuery() {
  return useQuery({
    queryKey: ["overview"],
    queryFn: fetchOverview,
    staleTime: 5_000,
    refetchInterval: 7_000,
  });
}

export function usePmConsolePositionQuery(targetKey: string) {
  return useQuery({
    queryKey: ["target-position", targetKey],
    queryFn: () => fetchTargetPosition(targetKey),
    enabled: targetKey.length > 0,
  });
}

export function usePmConsoleThesisRevisionsQuery(targetKey: string) {
  return useQuery({
    queryKey: ["target-thesis-revisions", targetKey],
    queryFn: () => fetchTargetThesisRevisions(targetKey),
    enabled: targetKey.length > 0,
  });
}

export function usePmConsoleThesisRevisionDetailQuery(
  targetKey: string,
  revisionId: string
) {
  return useQuery({
    queryKey: ["target-thesis-revision-detail", targetKey, revisionId],
    queryFn: () => fetchTargetThesisRevisionDetail(targetKey, revisionId),
    enabled: targetKey.length > 0 && revisionId.length > 0,
  });
}

export function usePmConsoleEpisodesQuery(
  targetKey: string,
  filters: {
    offset?: number;
    limit?: number;
    phase?: string | null;
    query?: string | null;
    anchorEpisodeId?: string | null;
  },
) {
  return useQuery({
    queryKey: [
      "target-episodes",
      targetKey,
      filters.offset ?? 0,
      filters.limit ?? null,
      filters.phase ?? null,
      filters.query ?? null,
      filters.anchorEpisodeId ?? null,
    ],
    queryFn: () => fetchTargetEpisodes(targetKey, {
      offset: filters.offset,
      limit: filters.limit,
      phase: filters.phase,
      query: filters.query,
      anchorEpisodeId: filters.anchorEpisodeId,
    }),
    enabled: targetKey.length > 0,
  });
}

export function usePmConsoleEpisodeDetailQuery(targetKey: string, episodeId: string) {
  return useQuery({
    queryKey: ["target-episode-detail", targetKey, episodeId],
    queryFn: () => fetchTargetEpisodeDetail(targetKey, episodeId),
    enabled: targetKey.length > 0 && episodeId.length > 0,
  });
}

export function usePmConsoleOperatorQuery(targetKey: string) {
  return useQuery({
    queryKey: ["target-operator", targetKey],
    queryFn: () => fetchTargetOperator(targetKey),
    enabled: targetKey.length > 0,
  });
}

export function usePmConsoleOperatorHistoryQuery(targetKey: string) {
  return useQuery({
    queryKey: ["target-operator-history", targetKey],
    queryFn: () => fetchTargetOperatorHistory(targetKey),
    enabled: targetKey.length > 0,
  });
}

export function usePmConsoleOperatorHistoryVersionQuery(
  targetKey: string,
  versionId: string | null,
) {
  return useQuery({
    queryKey: ["target-operator-history-version", targetKey, versionId],
    queryFn: () => fetchTargetOperatorHistoryVersion(targetKey, versionId ?? ""),
    enabled: targetKey.length > 0 && Boolean(versionId),
  });
}

export function usePmConsoleOperatorUpdateMutation(targetKey: string) {
  return useMutation({
    mutationKey: ["target-operator-update", targetKey],
    mutationFn: ({ contentMd, expectedContentSha256 }: {
      contentMd: string;
      expectedContentSha256: string;
    }) => updateTargetOperator(targetKey, contentMd, expectedContentSha256),
  });
}

export function usePmConsoleOperatorRestoreMutation(targetKey: string) {
  return useMutation({
    mutationKey: ["target-operator-restore", targetKey],
    mutationFn: ({ versionId, expectedContentSha256 }: {
      versionId: string;
      expectedContentSha256: string;
    }) => restoreTargetOperatorHistoryVersion(targetKey, versionId, expectedContentSha256),
  });
}

export function usePmConsolePmDecisionsQuery(targetKey: string) {
  return useQuery({
    queryKey: ["target-pm-decisions", targetKey],
    queryFn: () => fetchTargetPmDecisions(targetKey),
    enabled: targetKey.length > 0,
  });
}

export function usePmConsolePmDecisionDetailQuery(
  targetKey: string,
  decisionId: string
) {
  return useQuery({
    queryKey: ["target-pm-decision-detail", targetKey, decisionId],
    queryFn: () => fetchTargetPmDecisionDetail(targetKey, decisionId),
    enabled: targetKey.length > 0 && decisionId.length > 0,
  });
}

export function usePmConsoleAnalysisAssessmentQuery(
  targetKey: string,
  assessmentId: string
) {
  return useQuery({
    queryKey: ["target-analysis-assessment", targetKey, assessmentId],
    queryFn: () => fetchTargetAnalysisAssessment(targetKey, assessmentId),
    enabled: targetKey.length > 0 && assessmentId.length > 0,
  });
}

export function usePmConsoleThesisRevisionTranslationMutation(
  targetKey: string,
  revisionId: string
) {
  return useMutation({
    mutationKey: ["target-thesis-revision-translation", targetKey, revisionId],
    mutationFn: (request: PMConsoleThesisTranslationRequest) =>
      translateTargetThesisRevision(targetKey, revisionId, request),
  });
}

export function usePmConsoleThesisQuickBriefMutation(
  targetKey: string,
  revisionId: string
) {
  return useMutation({
    mutationKey: ["target-thesis-quick-brief", targetKey, revisionId],
    mutationFn: (canonicalBundleSha256: string) =>
      buildTargetThesisQuickBrief(targetKey, revisionId, {
        canonical_bundle_sha256: canonicalBundleSha256,
      }),
  });
}
