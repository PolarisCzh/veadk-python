// @vitest-environment jsdom
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { beforeEach, afterEach, expect, it, vi } from "vitest";
import { MpaCreateDialog } from "../src/ui/mpa-create/MpaCreateDialog";
import * as api from "../src/adk/mpaCreation";

vi.mock("../src/adk/mpaCreation", () => ({
  getMpaCreationConfig: vi.fn(),
  startMpaCreation: vi.fn(),
  getMpaCreation: vi.fn(),
  cancelMpaCreation: vi.fn(),
}));
vi.mock("react-i18next", () => {
  const t = (key: string) => key;
  return {
    useTranslation: () => ({ t }),
    initReactI18next: { type: "3rdParty", init: () => {} },
  };
});
let host: HTMLDivElement;
let root: ReturnType<typeof createRoot>;
beforeEach(() => {
  (
    globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }
  ).IS_REACT_ACT_ENVIRONMENT = true;
  sessionStorage.clear();
  vi.clearAllMocks();
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
});
async function mount() {
  await act(async () =>
    root.render(
      <MpaCreateDialog
        region="cn-beijing"
        onClose={() => {}}
        onCreated={() => {}}
      />,
    ),
  );
}
it("blocks creation when prerequisites are not configured", async () => {
  vi.mocked(api.getMpaCreationConfig).mockResolvedValue({
    configured: false,
    region: "cn-beijing",
    error: "Missing server profile",
  });
  await mount();
  expect(document.body.textContent).toContain("Missing server profile");
  const button = [...document.querySelectorAll("button")].find(
    (b) => b.textContent === "myAgents.mpaCreate.submit",
  );
  expect(button?.disabled).toBe(true);
});

it("uses a selected YAML only for this creation without persisting its contents", async () => {
  vi.mocked(api.getMpaCreationConfig).mockResolvedValue({
    configured: false,
    region: "cn-beijing",
    uploadAllowed: true,
    error: "Missing server profile",
  });
  vi.mocked(api.startMpaCreation).mockReturnValue(new Promise(() => {}));
  await mount();
  expect(submitButton().disabled).toBe(true);
  const yaml = "region: cn-beijing\nmanaged: {version: 1}\n";
  const file = new File([yaml], "create.yaml", { type: "text/yaml" });
  Object.defineProperty(file, "arrayBuffer", {
    value: async () => new TextEncoder().encode(yaml).buffer,
  });
  const picker = document.querySelector<HTMLInputElement>('input[type="file"]')!;
  await act(async () => {
    Object.defineProperty(picker, "files", { configurable: true, value: [file] });
    picker.dispatchEvent(new Event("change", { bubbles: true }));
  });
  expect(submitButton().disabled).toBe(false);
  await act(async () => submitButton().click());
  expect(api.startMpaCreation).toHaveBeenCalledWith(
    expect.objectContaining({ region: "cn-beijing" }),
    expect.any(AbortSignal),
    yaml,
  );
  expect(sessionStorage.getItem("mpa-create:cn-beijing")).not.toContain(yaml);
});

it("clears untouched server image defaults when YAML is selected", async () => {
  vi.mocked(api.getMpaCreationConfig).mockResolvedValue({
    configured: true,
    region: "cn-beijing",
    uploadAllowed: true,
    runtimeImage: "registry.example/mpa:v1",
    workerImage: "registry.example/worker:v1",
  });
  await mount();
  const file = new File(["managed: {}"], "create.yaml");
  const picker = document.querySelector<HTMLInputElement>('input[type="file"]')!;
  await act(async () => {
    Object.defineProperty(picker, "files", { configurable: true, value: [file] });
    picker.dispatchEvent(new Event("change", { bubbles: true }));
  });
  expect(field("runtimeImage").value).toBe("");
  expect(field("workerImage").value).toBe("");
});

it("retains a user-edited image when YAML is selected", async () => {
  vi.mocked(api.getMpaCreationConfig).mockResolvedValue({
    configured: true,
    region: "cn-beijing",
    uploadAllowed: true,
    runtimeImage: "registry.example/mpa:v1",
    workerImage: "registry.example/worker:v1",
  });
  await mount();
  await edit("runtimeImage", "registry.example/mpa:custom");
  const picker = document.querySelector<HTMLInputElement>('input[type="file"]')!;
  await act(async () => {
    Object.defineProperty(picker, "files", {
      configurable: true,
      value: [new File(["managed: {}"], "create.yaml")],
    });
    picker.dispatchEvent(new Event("change", { bubbles: true }));
  });
  expect(field("runtimeImage").value).toBe("registry.example/mpa:custom");
  expect(field("workerImage").value).toBe("");
});

it("rejects invalid files and invalid UTF-8 without submitting", async () => {
  vi.mocked(api.getMpaCreationConfig).mockResolvedValue({
    configured: false,
    region: "cn-beijing",
    uploadAllowed: true,
  });
  await mount();
  const picker = document.querySelector<HTMLInputElement>('input[type="file"]')!;
  await act(async () => {
    Object.defineProperty(picker, "files", {
      configurable: true,
      value: [new File(["not yaml"], "create.txt")],
    });
    picker.dispatchEvent(new Event("change", { bubbles: true }));
  });
  expect(submitButton().disabled).toBe(true);
  expect(document.body.textContent).toContain("myAgents.mpaCreate.uploadInvalid");
  const file = new File([new Uint8Array([0xff])], "create.yaml");
  Object.defineProperty(file, "arrayBuffer", {
    value: async () => new Uint8Array([0xff]).buffer,
  });
  await act(async () => {
    Object.defineProperty(picker, "files", { configurable: true, value: [file] });
    picker.dispatchEvent(new Event("change", { bubbles: true }));
  });
  await act(async () => submitButton().click());
  expect(api.startMpaCreation).not.toHaveBeenCalled();
  expect(document.body.textContent).toContain("myAgents.mpaCreate.uploadInvalidUtf8");
});

it("requires reselecting YAML after reopening a submitted upload", async () => {
  sessionStorage.setItem(
    "mpa-create:cn-beijing",
    JSON.stringify({
      input: {
        requestId: "11111111-1111-4111-8111-111111111111",
        agentId: "mi-test",
        description: "",
        region: "cn-beijing",
      },
      submitted: true,
      uploaded: true,
    }),
  );
  vi.mocked(api.getMpaCreationConfig).mockResolvedValue({
    configured: true,
    region: "cn-beijing",
    uploadAllowed: true,
  });
  await mount();
  expect(submitButton().disabled).toBe(true);
  const picker = document.querySelector<HTMLInputElement>('input[type="file"]')!;
  await act(async () => {
    Object.defineProperty(picker, "files", {
      configurable: true,
      value: [new File(["managed: {}"], "create.yaml")],
    });
    picker.dispatchEvent(new Event("change", { bubbles: true }));
  });
  expect(submitButton().disabled).toBe(false);
});

it("includes YAML only in the selected creation API request", async () => {
  const realApi = await vi.importActual<typeof import("../src/adk/mpaCreation")>(
    "../src/adk/mpaCreation",
  );
  const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => ({
    ok: true,
    json: async () => ({ taskId: "task-1", state: "running" }),
  }));
  vi.stubGlobal("fetch", fetchMock);
  try {
    const input = {
      requestId: "11111111-1111-4111-8111-111111111111",
      agentId: "mi-test",
      description: "",
      region: "cn-beijing",
    };
    await realApi.startMpaCreation(input, new AbortController().signal, "managed: {}\n");
    await realApi.startMpaCreation(input, new AbortController().signal);
    const first = JSON.parse(fetchMock.mock.calls[0][1].body as string);
    const second = JSON.parse(fetchMock.mock.calls[1][1].body as string);
    expect(first.configYaml).toBe("managed: {}\n");
    expect(second.configYaml).toBeUndefined();
  } finally {
    vi.unstubAllGlobals();
  }
});
it("persists identity before submission and prevents repeated clicks", async () => {
  vi.mocked(api.getMpaCreationConfig).mockResolvedValue({
    configured: true,
    region: "cn-beijing",
  });
  vi.mocked(api.startMpaCreation).mockReturnValue(new Promise(() => {}));
  await mount();
  const button = [...document.querySelectorAll("button")].find(
    (b) => b.textContent === "myAgents.mpaCreate.submit",
  )!;
  await act(async () => {
    button.click();
    button.click();
  });
  expect(api.startMpaCreation).toHaveBeenCalledTimes(1);
  expect(sessionStorage.getItem("mpa-create:cn-beijing")).toContain(
    "requestId",
  );
});

it("ignores late configuration after unmount", async () => {
  let resolve!: (value: api.MpaCreationConfig) => void;
  vi.mocked(api.getMpaCreationConfig).mockReturnValue(
    new Promise((r) => {
      resolve = r;
    }),
  );
  await mount();
  await act(async () => root.render(null));
  await act(async () => resolve({ configured: true, region: "cn-beijing" }));
  expect(document.querySelector('[role="dialog"]')).toBeNull();
  expect(api.startMpaCreation).not.toHaveBeenCalled();
});

it("keeps submitted identity fixed when the POST response is lost", async () => {
  vi.mocked(api.getMpaCreationConfig).mockResolvedValue({
    configured: true,
    region: "cn-beijing",
  });
  vi.mocked(api.startMpaCreation).mockRejectedValue(new Error("Response lost"));
  await mount();
  const button = [...document.querySelectorAll("button")].find(
    (b) => b.textContent === "myAgents.mpaCreate.submit",
  )!;
  await act(async () => button.click());
  const original = vi.mocked(api.startMpaCreation).mock.calls[0][0];
  expect(document.querySelector<HTMLInputElement>("input")?.disabled).toBe(
    true,
  );
  await act(async () => button.click());
  expect(vi.mocked(api.startMpaCreation).mock.calls[1][0]).toEqual(original);
});

it("can omit the shared modal footer without overriding component styles", async () => {
  const { ModalLayout } = await import("../src/components/layouts/ModalLayout");
  await act(async () =>
    root.render(
      <ModalLayout title="Test" footer={null}>
        Body
      </ModalLayout>,
    ),
  );
  expect(host.querySelector("footer")).toBeNull();
  await act(async () =>
    root.render(<ModalLayout title="Test">Body</ModalLayout>),
  );
  expect(host.querySelector("footer")?.textContent).toContain("Confirm");
});

function field(name: string) {
  return document.querySelector<HTMLInputElement>(`input[name="${name}"]`)!;
}
async function edit(name: string, value: string) {
  await act(async () => {
    Object.getOwnPropertyDescriptor(
      HTMLInputElement.prototype,
      "value",
    )!.set!.call(field(name), value);
    field(name).dispatchEvent(new Event("input", { bubbles: true }));
  });
}
function submitButton() {
  return [...document.querySelectorAll("button")].find(
    (b) => b.textContent === "myAgents.mpaCreate.submit",
  )!;
}
it("prefills both images and submits edits or an intentionally cleared default", async () => {
  vi.mocked(api.getMpaCreationConfig).mockResolvedValue({
    configured: true,
    region: "cn-beijing",
    runtimeImage: "registry.example/mpa:v1",
    workerImage: "registry.example/worker:v1",
  });
  vi.mocked(api.startMpaCreation).mockReturnValue(new Promise(() => {}));
  await mount();
  expect(field("runtimeImage").value).toBe("registry.example/mpa:v1");
  expect(field("workerImage").value).toBe("registry.example/worker:v1");
  await edit("runtimeImage", "registry.example/mpa:custom");
  await edit("workerImage", "");
  await act(async () => submitButton().click());
  expect(vi.mocked(api.startMpaCreation).mock.calls[0][0]).toMatchObject({
    runtimeImage: "registry.example/mpa:custom",
    workerImage: "",
  });
  expect(field("runtimeImage").disabled).toBe(true);
});
it("blocks malformed image references but allows empty values", async () => {
  vi.mocked(api.getMpaCreationConfig).mockResolvedValue({
    configured: true,
    region: "cn-beijing",
  });
  await mount();
  await edit("runtimeImage", "https://repo?token=private");
  expect(submitButton().disabled).toBe(true);
  expect(field("runtimeImage").getAttribute("aria-invalid")).toBe("true");
  await edit("runtimeImage", "");
  expect(submitButton().disabled).toBe(false);
});
it("does not replace saved or cleared image choices when config reloads after a lost response", async () => {
  vi.mocked(api.getMpaCreationConfig).mockResolvedValue({
    configured: true,
    region: "cn-beijing",
    runtimeImage: "registry.example/mpa:v1",
    workerImage: "registry.example/worker:v1",
  });
  vi.mocked(api.startMpaCreation).mockRejectedValue(new Error("Response lost"));
  await mount();
  await edit("runtimeImage", "");
  await edit("workerImage", "registry.example/worker:custom");
  await act(async () => submitButton().click());
  const original = vi.mocked(api.startMpaCreation).mock.calls[0][0];
  await act(async () => root.render(null));
  vi.mocked(api.getMpaCreationConfig).mockResolvedValue({
    configured: true,
    region: "cn-beijing",
    runtimeImage: "registry.example/mpa:v2",
  });
  await mount();
  expect(field("runtimeImage").value).toBe("");
  expect(field("workerImage").value).toBe("registry.example/worker:custom");
  await act(async () => submitButton().click());
  expect(vi.mocked(api.startMpaCreation).mock.calls[1][0]).toEqual(original);
});
