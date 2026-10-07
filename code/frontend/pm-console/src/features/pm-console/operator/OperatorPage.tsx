import { useEffect, useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { indentWithTab } from "@codemirror/commands";
import { markdown } from "@codemirror/lang-markdown";
import { EditorView, keymap } from "@codemirror/view";
import CodeMirror from "@uiw/react-codemirror";
import { useParams } from "react-router-dom";
import { MarkdownContent } from "../../../components/MarkdownContent";
import { PMConsoleApiError } from "../../../api";
import type { PMConsoleOperatorConflict } from "../../../types";
import {
  usePmConsoleOperatorHistoryQuery,
  usePmConsoleOperatorHistoryVersionQuery,
  usePmConsoleOperatorQuery,
  usePmConsoleOperatorRestoreMutation,
  usePmConsoleOperatorUpdateMutation,
} from "../hooks";
import { useLocale } from "../locale/LocaleProvider";

const draftByTarget = new Map<string, string>();

export function resetOperatorContextDraftsForTest() {
  draftByTarget.clear();
}

export function OperatorPage() {
  const { t } = useLocale();
  const { target: routeTarget } = useParams<{ target: string }>();
  const target = routeTarget ? decodeURIComponent(routeTarget) : "";
  const queryClient = useQueryClient();
  const operatorQuery = usePmConsoleOperatorQuery(target);
  const historyQuery = usePmConsoleOperatorHistoryQuery(target);
  const [draft, setDraft] = useState(() => draftByTarget.get(target) ?? "");
  const [selectedVersion, setSelectedVersion] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const historyDetailQuery = usePmConsoleOperatorHistoryVersionQuery(
    target,
    selectedVersion,
  );
  const updateMutation = usePmConsoleOperatorUpdateMutation(target);
  const restoreMutation = usePmConsoleOperatorRestoreMutation(target);

  useEffect(() => {
    setDraft(draftByTarget.get(target) ?? "");
    setSelectedVersion(null);
    setNotice(null);
  }, [target]);

  useEffect(() => {
    if (operatorQuery.data && !draftByTarget.has(target)) {
      draftByTarget.set(target, operatorQuery.data.content_md);
      setDraft(operatorQuery.data.content_md);
    }
  }, [operatorQuery.data, target]);

  const current = operatorQuery.data;
  const conflict = getConflict(updateMutation.error);
  const selectedVersionSummary = useMemo(
    () => historyQuery.data?.versions.find((version) => version.version_id === selectedVersion) ?? null,
    [historyQuery.data?.versions, selectedVersion],
  );

  if (!target) return <div className="route-empty">{t("No target selected.")}</div>;
  if (operatorQuery.isLoading || historyQuery.isLoading) {
    return <div className="route-empty">{t("Loading Operator Context...")}</div>;
  }
  if (operatorQuery.isError || historyQuery.isError || !current) {
    return <div className="route-empty">{t("Operator Context is unavailable for")} `{target}`.</div>;
  }
  const currentDocument = current;
  const isDirty = draft !== currentDocument.content_md;

  function changeDraft(value: string) {
    draftByTarget.set(target, value);
    setDraft(value);
    setNotice(null);
  }

  function save() {
    updateMutation.mutate(
      { contentMd: draft, expectedContentSha256: currentDocument.content_sha256 },
      {
        onSuccess: (updated) => {
          queryClient.setQueryData(["target-operator", target], updated);
          draftByTarget.set(target, updated.content_md);
          setDraft(updated.content_md);
          setNotice(t("Operator Context saved. Analysis will read this body at its next start."));
          void queryClient.invalidateQueries({ queryKey: ["target-operator-history", target] });
        },
      },
    );
  }

  function reloadLatest() {
    if (!conflict) return;
    draftByTarget.set(target, conflict.current.content_md);
    setDraft(conflict.current.content_md);
    setNotice(t("Latest Operator Context loaded; your previous draft was discarded."));
    updateMutation.reset();
  }

  function keepDraft() {
    if (!conflict) return;
    queryClient.setQueryData(["target-operator", target], conflict.current);
    setNotice(t("Your draft is kept; review it against the latest active context before saving again."));
    updateMutation.reset();
  }

  function restore() {
    if (!selectedVersion || !historyDetailQuery.data) return;
    restoreMutation.mutate(
      {
        versionId: selectedVersion,
        expectedContentSha256: currentDocument.content_sha256,
      },
      {
        onSuccess: (updated) => {
          draftByTarget.set(target, updated.content_md);
          setDraft(updated.content_md);
          setNotice(t("Version restored. The former active body is now recoverable history."));
          void queryClient.invalidateQueries({ queryKey: ["target-operator", target] });
          void queryClient.invalidateQueries({ queryKey: ["target-operator-history", target] });
        },
      },
    );
  }

  return (
    <div className="operator-page">
      <header className="operator-hero">
        <div>
          <p className="eyebrow">{t("Operator Context")}</p>
          <h2>{target.toUpperCase()} {t("Operator Context")}</h2>
          <p className="operator-subtitle">
            {t("Inject and maintain the human system view for this target. Analysis reads this body as an exact snapshot at its next start.")}
          </p>
        </div>
        <div className="operator-status" data-status={currentDocument.status}>
          <span>{t(statusLabel(currentDocument.status))}</span>
          <small>{isDirty ? t("Unsaved draft") : t("Saved")}</small>
        </div>
      </header>

      {notice ? <div className="operator-notice" role="status">{notice}</div> : null}
      {conflict ? (
        <div className="operator-conflict" role="alert">
          <strong>{t("This Operator Context is stale.")}</strong>
          <span>{t("Another write changed the active body. Your draft is still in the editor.")}</span>
          <div className="operator-conflict-actions">
            <button type="button" className="operator-button" onClick={keepDraft}>
              {t("Keep my draft")}
            </button>
            <button type="button" className="operator-button operator-button-secondary" onClick={reloadLatest}>
              {t("Reload latest")}
            </button>
          </div>
        </div>
      ) : null}

      <div className="operator-layout">
        <section className="operator-editor-panel" aria-label={t("Edit Operator Context")}>
          <div className="operator-panel-header">
            <div>
              <p className="eyebrow">{t("Active body")}</p>
              <h3>{t("Write the context you want Analysis to see")}</h3>
            </div>
            <button
              type="button"
              className="operator-button"
              disabled={!isDirty || updateMutation.isPending}
              onClick={save}
            >
              {updateMutation.isPending ? t("Saving...") : t("Save Operator Context")}
            </button>
          </div>
          <div className="operator-editor-shell">
            <CodeMirror
              value={draft}
              height="520px"
              extensions={[
                markdown(),
                keymap.of([indentWithTab]),
                EditorView.contentAttributes.of({
                  "aria-label": t("Operator Context Markdown editor"),
                }),
              ]}
              basicSetup={{ lineNumbers: true, foldGutter: true, highlightActiveLine: true }}
              onChange={changeDraft}
            />
          </div>
          <p className="operator-editor-footnote">
            {currentDocument.status === "missing" ? t("No active file exists yet; saving will create it.") : t("Markdown is stored exactly as written.")}
          </p>
        </section>

        <section className="operator-preview-panel" aria-label={t("Rendered Operator Context preview")}>
          <div className="operator-panel-header">
            <div>
              <p className="eyebrow">{t("Reading preview")}</p>
              <h3>{t("Current draft")}</h3>
            </div>
            <span className="operator-hash" title={currentDocument.content_sha256}>{currentDocument.content_sha256.slice(0, 12)}...</span>
          </div>
          <div className="operator-preview">
            {draft.trim() ? <MarkdownContent content={draft} className="operator-markdown" /> : <p className="operator-empty">{t("This context is empty and ready to edit.")}</p>}
          </div>
        </section>

        <aside className="operator-history-panel" aria-label={t("Operator Context history")}>
          <div className="operator-panel-header">
            <div>
              <p className="eyebrow">{t("Recoverable history")}</p>
              <h3>{t("Versions")}</h3>
            </div>
            <span className="operator-count">{historyQuery.data?.versions.length ?? 0}</span>
          </div>
          {historyQuery.data?.versions.length ? (
            <div className="operator-history-list">
              {historyQuery.data.versions.map((version) => (
                <button
                  type="button"
                  className={selectedVersion === version.version_id ? "operator-history-item selected" : "operator-history-item"}
                  key={version.version_id}
                  onClick={() => setSelectedVersion(version.version_id)}
                >
                  <span>{version.version_id.slice(0, 16)}...</span>
                  <small>{version.version_id}</small>
                </button>
              ))}
            </div>
          ) : <p className="operator-empty">{t("No previous non-empty body has been saved.")}</p>}
          {selectedVersionSummary ? (
            <div className="operator-history-detail">
              <div className="operator-panel-header">
                <div>
                  <p className="eyebrow">{t("Selected version")}</p>
                  <h3>{selectedVersionSummary.version_id.slice(0, 12)}...</h3>
                </div>
                <button
                  type="button"
                  className="operator-button operator-button-secondary"
                  disabled={restoreMutation.isPending || !historyDetailQuery.data}
                  onClick={restore}
                >
                  {restoreMutation.isPending ? t("Restoring...") : t("Restore version")}
                </button>
              </div>
              <p className="operator-restore-note">{t("Restoring preserves the current active body in recoverable history first.")}</p>
              {historyDetailQuery.isLoading ? <p className="operator-empty">{t("Loading selected version...")}</p> : historyDetailQuery.data ? <MarkdownContent content={historyDetailQuery.data.content_md ?? ""} className="operator-markdown" /> : <p className="operator-empty">{t("Selected version unavailable.")}</p>}
            </div>
          ) : null}
        </aside>
      </div>
    </div>
  );
}

function statusLabel(status: "missing" | "empty" | "ready") {
  if (status === "missing") return "Missing active page";
  if (status === "empty") return "Empty active page";
  return "Active page ready";
}

function getConflict(error: unknown): PMConsoleOperatorConflict | null {
  if (!(error instanceof PMConsoleApiError) || error.status !== 409) return null;
  return (error.payload as PMConsoleOperatorConflict | null) ?? null;
}
