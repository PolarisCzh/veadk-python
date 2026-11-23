// @vitest-environment jsdom
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { studioFetch } from "../src/adk/client";
import {
  FeishuBotInviteAdmin,
  FeishuBotInvitePage,
} from "../src/ui/FeishuBotInvite";

vi.mock("../src/adk/client", () => ({ studioFetch: vi.fn() }));
vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));
vi.mock("@openai/apps-sdk-ui/components/Button", () => ({
  Button: ({ color: _color, size: _size, variant: _variant, ...props }) => (
    <button {...props} />
  ),
}));

const token = "a".repeat(43);
const qr = "data:image/png;base64,AAAB";
const fetchInvite = vi.fn<typeof fetch>();
let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  vi.stubGlobal("React", React);
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.stubGlobal("fetch", fetchInvite);
  fetchInvite.mockReset();
  vi.mocked(studioFetch).mockReset();
  sessionStorage.clear();
  window.history.replaceState({}, "", `/feishu-bot-invite#${token}`);
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

it("automatically redeems with an anonymous request and polls the same attempt", async () => {
  vi.useFakeTimers();
  fetchInvite.mockResolvedValueOnce(
    Response.json({ status: "pending", pollAfterSeconds: 3, qr }),
  );
  fetchInvite.mockResolvedValueOnce(Response.json({ status: "bound" }));
  await act(async () => root.render(<FeishuBotInvitePage />));
  expect(container.querySelector("img")?.getAttribute("src")).toBe(qr);
  const request = fetchInvite.mock.calls[0]?.[1];
  expect(request?.credentials).toBe("omit");
  expect(JSON.parse(String(request?.body))).toEqual({
    token,
    attempt: expect.stringMatching(/^[A-Za-z0-9_-]{43}$/),
  });
  await act(async () => vi.advanceTimersByTimeAsync(3000));
  expect(fetchInvite.mock.calls[1]?.[0]).toBe("/web/feishu-bot-invite/status");
  expect(fetchInvite.mock.calls[1]?.[1]?.body).toBe(request?.body);
  expect(container.textContent).toContain("inviteState_bound");
  expect(container.querySelector("img")).toBeNull();
});

it("shows a transient failure and retries with the original attempt", async () => {
  fetchInvite.mockResolvedValueOnce(Response.json(null, { status: 502 }));
  fetchInvite.mockResolvedValueOnce(
    Response.json({ status: "pending", pollAfterSeconds: 3, qr }),
  );
  await act(async () => root.render(<FeishuBotInvitePage />));
  expect(container.textContent).toContain("inviteState_error");
  await act(async () => container.querySelector("button")?.click());
  expect(container.querySelector("img")?.getAttribute("src")).toBe(qr);
  expect(fetchInvite.mock.calls[0]?.[1]?.body).toBe(
    fetchInvite.mock.calls[1]?.[1]?.body,
  );
});

it("does not call the server for a malformed link", async () => {
  window.history.replaceState({}, "", "/feishu-bot-invite#invalid");
  await act(async () => root.render(<FeishuBotInvitePage />));
  expect(container.textContent).toContain("inviteState_invalid");
  expect(fetchInvite).not.toHaveBeenCalled();
});

it("creates and revokes a bounded invitation for the selected Runtime", async () => {
  const adminFetch = vi.mocked(studioFetch);
  adminFetch.mockResolvedValueOnce(Response.json({ token }));
  adminFetch.mockResolvedValueOnce(new Response(null, { status: 204 }));
  await act(async () =>
    root.render(<FeishuBotInviteAdmin runtimeId="r-one" region="cn-beijing" />),
  );
  await act(async () => container.querySelector("button")?.click());
  await act(async () =>
    container
      .querySelector("form")
      ?.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })),
  );
  expect(JSON.parse(String(adminFetch.mock.calls[0]?.[1]?.body))).toEqual({
    runtimeId: "r-one",
    region: "cn-beijing",
    maxUses: 10,
    validHours: 24,
  });
  const link = container.querySelector('input[type="text"]');
  expect(link?.getAttribute("value")).toContain(`/feishu-bot-invite#${token}`);
  const revoke = Array.from(container.querySelectorAll("button")).find(
    (button) => button.textContent?.includes("inviteRevoke"),
  );
  await act(async () => revoke?.click());
  expect(adminFetch.mock.calls[1]?.[0]).toBe("/web/feishu-bot-invites/revoke");
  expect(container.textContent).toContain("inviteRevoked");
});

it("starts a new attempt when opening a different invite in the same tab", async () => {
  fetchInvite.mockResolvedValueOnce(Response.json({ status: "bound" }));
  fetchInvite.mockResolvedValueOnce(
    Response.json({ status: "pending", pollAfterSeconds: 3, qr }),
  );
  await act(async () => root.render(<FeishuBotInvitePage />));
  const next = "b".repeat(43);
  await act(async () => {
    window.history.replaceState({}, "", `/feishu-bot-invite#${next}`);
    window.dispatchEvent(new Event("hashchange"));
  });
  const previous = JSON.parse(String(fetchInvite.mock.calls[0]?.[1]?.body));
  const current = JSON.parse(String(fetchInvite.mock.calls[1]?.[1]?.body));
  expect(current.token).toBe(next);
  expect(current.attempt).not.toBe(previous.attempt);
  expect(container.querySelector("img")?.getAttribute("src")).toBe(qr);
});

it("waits for a visible tab and reuses the saved attempt while the QR is starting", async () => {
  const visible = vi
    .spyOn(document, "visibilityState", "get")
    .mockReturnValue("hidden");
  vi.useFakeTimers();
  try {
    const attempt = "s".repeat(43);
    sessionStorage.setItem(`feishu-bot-invite:${token}`, attempt);
    fetchInvite.mockResolvedValueOnce(
      Response.json({ status: "starting", pollAfterSeconds: 3 }),
    );
    fetchInvite.mockResolvedValueOnce(
      Response.json({ status: "pending", pollAfterSeconds: 3, qr }),
    );
    await act(async () => root.render(<FeishuBotInvitePage />));
    expect(fetchInvite).not.toHaveBeenCalled();
    visible.mockReturnValue("visible");
    await act(async () =>
      document.dispatchEvent(new Event("visibilitychange")),
    );
    expect(container.textContent).toContain("inviteLoading");
    await act(async () => vi.advanceTimersByTimeAsync(3000));
    expect(fetchInvite.mock.calls[1]?.[0]).toBe(
      "/web/feishu-bot-invite/redeem",
    );
    expect(
      JSON.parse(String(fetchInvite.mock.calls[1]?.[1]?.body)).attempt,
    ).toBe(attempt);
    expect(container.querySelector("img")?.getAttribute("src")).toBe(qr);
  } finally {
    visible.mockRestore();
  }
});

it.each([
  [Response.json(null, { status: 404 }), "inviteState_invalid"],
  [Response.json({ status: "unrecognized" }), "inviteState_error"],
  [Response.json({ status: "revoked" }), "inviteState_revoked"],
  [Response.json({ status: "full" }), "inviteState_full"],
])("renders the invite response safely", async (response, message) => {
  fetchInvite.mockResolvedValueOnce(response);
  await act(async () => root.render(<FeishuBotInvitePage />));
  expect(container.textContent).toContain(message);
  expect(container.querySelector("img")).toBeNull();
});

function inviteButton(key: string) {
  return Array.from(container.querySelectorAll("button")).find((button) =>
    button.textContent?.includes(key),
  );
}

it("copies a generated link and lets an admin paste a link for revocation", async () => {
  const writeText = vi.fn<(link: string) => Promise<void>>();
  vi.stubGlobal("navigator", { clipboard: { writeText } });
  vi.mocked(studioFetch).mockResolvedValueOnce(Response.json({ token }));
  await act(async () =>
    root.render(<FeishuBotInviteAdmin runtimeId="r-one" region="cn-beijing" />),
  );
  await act(async () => container.querySelector("button")?.click());
  await act(async () =>
    container
      .querySelector("form")
      ?.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })),
  );
  writeText.mockResolvedValueOnce(undefined);
  await act(async () => inviteButton("inviteCopy")?.click());
  expect(writeText).toHaveBeenCalledWith(expect.stringContaining(`#${token}`));
  expect(container.textContent).toContain("inviteCopied");
  writeText.mockRejectedValueOnce(new Error("clipboard permission"));
  await act(async () => inviteButton("inviteCopy")?.click());
  expect(container.textContent).toContain("inviteCopyManually");
  const input = container.querySelector('input[type="text"]');
  await act(async () => {
    Object.getOwnPropertyDescriptor(
      HTMLInputElement.prototype,
      "value",
    )?.set?.call(input, "invalid");
    input?.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await act(async () => inviteButton("inviteRevoke")?.click());
  expect(container.textContent).toContain("inviteInvalidLink");
});

it.each([
  [
    () => Promise.resolve(Response.json(null, { status: 503 })),
    "inviteStorageUnavailable",
  ],
  [
    () => Promise.resolve(Response.json({ token: "invalid" })),
    "inviteAdminError",
  ],
  [() => Promise.reject(new Error("offline")), "inviteAdminError"],
])("reports invite creation failures", async (response, message) => {
  vi.mocked(studioFetch).mockImplementationOnce(response);
  await act(async () =>
    root.render(<FeishuBotInviteAdmin runtimeId="r-one" region="cn-beijing" />),
  );
  await act(async () => container.querySelector("button")?.click());
  await act(async () =>
    container
      .querySelector("form")
      ?.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })),
  );
  expect(container.textContent).toContain(message);
});
