import "@testing-library/jest-dom/vitest";
import { QueryClient, QueryClientProvider, useQueryClient } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { EditorView } from "@codemirror/view";
import type {
  PMConsoleOperatorDocument,
  PMConsoleOperatorHistoryResponse,
  PMConsoleOperatorHistoryVersion,
} from "../../../types";
import { OperatorPage, resetOperatorContextDraftsForTest } from "./OperatorPage";

const state = vi.hoisted(() => ({
  document: undefined as PMConsoleOperatorDocument | undefined,
  history: undefined as PMConsoleOperatorHistoryResponse | undefined,
  detail: undefined as PMConsoleOperatorHistoryVersion | undefined,
}));

const mutations = vi.hoisted(() => ({
  update: vi.fn(),
  restore: vi.fn(),
  updateReset: vi.fn(),
}));

vi.mock("../hooks", () => ({
  usePmConsoleOperatorQuery: (target: string) => {
    const queryClient = useQueryClient();
    return {
      isLoading: false,
      isError: false,
      data: queryClient.getQueryData<PMConsoleOperatorDocument>(["target-operator", target]) ?? state.document,
    };
  },
  usePmConsoleOperatorHistoryQuery: () => ({
    isLoading: false,
    isError: false,
    data: state.history,
  }),
  usePmConsoleOperatorHistoryVersionQuery: () => ({
    isLoading: false,
    isError: false,
    data: state.detail,
  }),
  usePmConsoleOperatorUpdateMutation: () => ({
    isPending: false,
    error: null,
    mutate: mutations.update,
    reset: mutations.updateReset,
  }),
  usePmConsoleOperatorRestoreMutation: () => ({
    isPending: false,
    error: null,
    mutate: mutations.restore,
  }),
}));

function renderPage(target = "sox") {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const view = render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[`/targets/${target}/operator`]}>
        <Routes>
          <Route path="/targets/:target/operator" element={<OperatorPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...view, queryClient };
}

beforeEach(() => {
  resetOperatorContextDraftsForTest();
  const firstBody = "# Earlier\n\nA recoverable body.\n";
  state.document = {
    target_key: "sox",
    page_path: "targets/sox/operator.md",
    content_md: "# Active\n\nCurrent operator frame.\n",
    content_sha256: "a".repeat(64),
    status: "ready",
    editable: true,
  };
  state.history = {
    target_key: "sox",
    versions: [{
      target_key: "sox",
      version_id: "b".repeat(64),
      content_sha256: "b".repeat(64),
    }],
  };
  state.detail = {
    target_key: "sox",
    version_id: "b".repeat(64),
    content_sha256: "b".repeat(64),
    content_md: firstBody,
  };
  mutations.update.mockImplementation((_request, options) => {
    options.onSuccess({
      ...state.document,
      content_md: "# Saved by server\n",
      content_sha256: "c".repeat(64),
      status: "ready",
    });
  });
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("Operator Context page", () => {
  it("renders the editor and preview, then saves an edited draft", async () => {
    renderPage();
    const editor = screen.getByRole("textbox", { name: "Operator Context Markdown editor" });
    expect(editor).toHaveTextContent("# Active");
    expect(
      within(screen.getByRole("region", { name: "Rendered Operator Context preview" })).getByText(
        "Current operator frame.",
        { exact: true },
      ),
    ).toBeInTheDocument();

    replaceEditorText(editor, "# Draft\n\nUpdated frame.");
    await waitFor(() => expect(screen.getByText("Unsaved draft", { exact: true })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Save Operator Context" }));
    expect(mutations.update).toHaveBeenCalledWith(
      { contentMd: "# Draft\n\nUpdated frame.", expectedContentSha256: "a".repeat(64) },
      expect.anything(),
    );
  });

  it("updates the saved cache after success and uses the returned hash on the next save", async () => {
    const view = renderPage("save-target");
    const editor = screen.getByRole("textbox", { name: "Operator Context Markdown editor" });

    replaceEditorText(editor, "# First draft\n");
    await waitFor(() => expect(screen.getByText("Unsaved draft", { exact: true })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Save Operator Context" }));

    await waitFor(() => expect(screen.getByText("Saved", { exact: true })).toBeInTheDocument());
    expect(view.queryClient.getQueryData<PMConsoleOperatorDocument>(["target-operator", "save-target"])?.content_sha256).toBe("c".repeat(64));

    replaceEditorText(editor, "# Second draft\n");
    await waitFor(() => expect(screen.getByText("Unsaved draft", { exact: true })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Save Operator Context" }));
    expect(mutations.update).toHaveBeenLastCalledWith(
      expect.objectContaining({ expectedContentSha256: "c".repeat(64) }),
      expect.anything(),
    );
  });

  it("shows the selected history body and makes restore explicit", () => {
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: /b{16}/ }));
    expect(screen.getByText("A recoverable body.", { exact: true })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Restore version" }));
    expect(mutations.restore).toHaveBeenCalledWith(
      { versionId: "b".repeat(64), expectedContentSha256: "a".repeat(64) },
      expect.anything(),
    );
    expect(screen.getByText(/current active body in recoverable history/i)).toBeInTheDocument();
  });
});

function replaceEditorText(editor: HTMLElement, content: string) {
  const view = EditorView.findFromDOM(editor);
  if (!view) throw new Error("CodeMirror editor view was not found.");
  view.dispatch({
    changes: { from: 0, to: view.state.doc.length, insert: content },
  });
}
