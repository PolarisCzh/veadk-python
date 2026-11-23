import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "@openai/apps-sdk-ui/components/Button";
import { studioFetch } from "../adk/client";
import { TextShimmer } from "./text-shimmer/TextShimmer";
import "./FeishuBotInvite.css";

const publicApi = "/web/feishu-bot-invite";
const adminApi = "/web/feishu-bot-invites";
export const feishuBotInvitePath = "/feishu-bot-invite";
const tokenPattern = /^[A-Za-z0-9_-]{43}$/;

function makeAttempt() {
  const bytes = window.crypto.getRandomValues(new Uint8Array(32));
  return btoa(String.fromCharCode(...bytes))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=/g, "");
}

function browserAttempt(token: string) {
  const storageKey = `feishu-bot-invite:${token}`;
  const attempt = makeAttempt();
  try {
    const old = sessionStorage.getItem(storageKey);
    if (old && tokenPattern.test(old)) return old;
    sessionStorage.setItem(storageKey, attempt);
  } catch {
    // The current page can still finish the binding without browser storage.
  }
  return attempt;
}

type InviteResult =
  | { status: "pending" | "starting"; pollAfterSeconds: number; qr?: string }
  | {
      status:
        | "bound"
        | "uncertain"
        | "expired"
        | "invite_expired"
        | "full"
        | "revoked";
    };
type PageState = InviteResult["status"] | "loading" | "invalid" | "error";

function parseResult(value: unknown): InviteResult {
  if (value && typeof value === "object" && "status" in value) {
    switch (value.status) {
      case "bound":
      case "uncertain":
      case "expired":
      case "invite_expired":
      case "full":
      case "revoked":
        return { status: value.status };
      case "pending":
      case "starting":
        if (
          "pollAfterSeconds" in value &&
          typeof value.pollAfterSeconds === "number" &&
          Number.isFinite(value.pollAfterSeconds)
        ) {
          const qr =
            "qr" in value &&
            typeof value.qr === "string" &&
            /^data:image\/png;base64,[A-Za-z0-9+/=]+$/.test(value.qr)
              ? value.qr
              : undefined;
          return {
            status: value.status,
            pollAfterSeconds: value.pollAfterSeconds,
            qr,
          };
        }
    }
  }
  throw new Error("Invalid invite status");
}

export function FeishuBotInvitePage() {
  const [token, setToken] = useState(() => window.location.hash.slice(1));
  useEffect(() => {
    const update = () => setToken(window.location.hash.slice(1));
    window.addEventListener("hashchange", update);
    return () => window.removeEventListener("hashchange", update);
  }, []);
  return <InviteRedemption key={token} token={token} />;
}

function InviteRedemption({ token }: { token: string }) {
  const { t } = useTranslation("ui");
  const [attempt] = useState(() =>
    tokenPattern.test(token) ? browserAttempt(token) : "",
  );
  const [state, setState] = useState<PageState>(
    attempt ? "loading" : "invalid",
  );
  const [qr, setQr] = useState<string>();
  const [retry, setRetry] = useState(0);

  useEffect(() => {
    if (!attempt) return;
    const controller = new AbortController();
    let timer: number | undefined;
    async function run(action: "redeem" | "status") {
      try {
        const response = await fetch(`${publicApi}/${action}`, {
          method: "POST",
          credentials: "omit",
          signal: controller.signal,
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ token, attempt }),
        });
        if (controller.signal.aborted) return;
        if (response.status === 404) {
          setState("invalid");
          return;
        }
        if (!response.ok) throw new Error("Invite request failed");
        const result = parseResult(await response.json());
        if (controller.signal.aborted) return;
        setState(result.status);
        if (result.status === "pending" || result.status === "starting") {
          if (result.qr) setQr(result.qr);
          timer = window.setTimeout(
            () => void run(result.status === "starting" ? "redeem" : "status"),
            Math.max(3, Math.min(result.pollAfterSeconds, 60)) * 1000,
          );
        }
      } catch {
        if (!controller.signal.aborted) setState("error");
      }
    }
    function startWhenVisible() {
      if (document.visibilityState !== "hidden") {
        document.removeEventListener("visibilitychange", startWhenVisible);
        void run("redeem");
      }
    }
    document.addEventListener("visibilitychange", startWhenVisible);
    startWhenVisible();
    return () => {
      controller.abort();
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", startWhenVisible);
    };
  }, [attempt, token, retry]);

  const waiting = state === "loading" || state === "starting";
  const retryable =
    state === "full" ||
    state === "expired" ||
    state === "uncertain" ||
    state === "error";
  return (
    <main className="feishu-invite-page">
      <section className="feishu-invite-card" aria-busy={waiting}>
        <h1>{t("channels.inviteTitle")}</h1>
        {state === "pending" && qr && (
          <img
            width={240}
            height={240}
            src={qr}
            alt={t("channels.inviteQrAlt")}
          />
        )}
        <p role="status" aria-live="polite">
          {waiting ? (
            <TextShimmer>{t("channels.inviteLoading")}</TextShimmer>
          ) : (
            t(`channels.inviteState_${state}`)
          )}
        </p>
        {retryable && (
          <Button
            color="secondary"
            variant="outline"
            onClick={() => {
              setQr(undefined);
              setState("loading");
              setRetry((value) => value + 1);
            }}
          >
            {t("channels.inviteRetry")}
          </Button>
        )}
      </section>
    </main>
  );
}

export function FeishuBotInviteAdmin({
  runtimeId,
  region,
}: {
  runtimeId: string;
  region: string;
}) {
  const { t } = useTranslation("ui");
  const id = useId();
  const [open, setOpen] = useState(false);
  const [link, setLink] = useState("");
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState("");
  const request = useRef<AbortController | null>(null);

  useEffect(() => () => request.current?.abort(), []);

  async function send(body: object, revoke = false) {
    if (request.current) return;
    const controller = new AbortController();
    request.current = controller;
    setBusy(true);
    setFeedback("");
    try {
      const response = await studioFetch(
        revoke ? `${adminApi}/revoke` : adminApi,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          signal: controller.signal,
          body: JSON.stringify({ runtimeId, region, ...body }),
        },
      );
      if (!response.ok) {
        setFeedback(
          response.status === 503
            ? "inviteStorageUnavailable"
            : "inviteAdminError",
        );
        return;
      }
      if (revoke) {
        setLink("");
        setFeedback("inviteRevoked");
        return;
      }
      const data: unknown = await response.json();
      if (
        data &&
        typeof data === "object" &&
        "token" in data &&
        typeof data.token === "string" &&
        tokenPattern.test(data.token)
      ) {
        setLink(
          `${window.location.origin}${feishuBotInvitePath}#${data.token}`,
        );
      } else {
        setFeedback("inviteAdminError");
      }
    } catch {
      if (!controller.signal.aborted) setFeedback("inviteAdminError");
    } finally {
      request.current = null;
      if (!controller.signal.aborted) setBusy(false);
    }
  }

  function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    void send({
      maxUses: Number(form.get("maxUses")),
      validHours: Number(form.get("validHours")),
    });
  }

  function revoke() {
    const match = link.trim().match(/#([A-Za-z0-9_-]{43})$/);
    if (match) void send({ token: match[1] }, true);
    else setFeedback("inviteInvalidLink");
  }

  async function copy() {
    try {
      await navigator.clipboard.writeText(link);
      setFeedback("inviteCopied");
    } catch {
      setFeedback("inviteCopyManually");
    }
  }

  return (
    <div className="runtime-channels runtime-channels-form">
      <Button
        color="secondary"
        size="sm"
        variant="outline"
        aria-expanded={open}
        aria-controls={id}
        onClick={() => setOpen((value) => !value)}
      >
        {t("channels.inviteAdminTitle")}
      </Button>
      {open && (
        <div className="feishu-invite-admin" id={id}>
          <form onSubmit={create}>
            <label>
              {t("channels.inviteMaxUses")}
              <input
                required
                type="number"
                name="maxUses"
                min={1}
                max={100}
                defaultValue={10}
              />
            </label>
            <label>
              {t("channels.inviteHours")}
              <input
                required
                type="number"
                name="validHours"
                min={1}
                max={168}
                defaultValue={24}
              />
            </label>
            <Button type="submit" size="sm" color="primary" disabled={busy}>
              {t("channels.inviteCreate")}
            </Button>
          </form>
          <label>
            {t("channels.inviteLink")}
            <input
              type="text"
              value={link}
              onChange={(event) => setLink(event.target.value)}
              autoComplete="off"
              spellCheck={false}
            />
          </label>
          <div className="runtime-channels-account-actions">
            <Button
              color="secondary"
              size="sm"
              variant="outline"
              disabled={busy || !link}
              onClick={() => void copy()}
            >
              {t("channels.inviteCopy")}
            </Button>
            <Button
              color="secondary"
              size="sm"
              variant="outline"
              disabled={busy || !link}
              onClick={revoke}
            >
              {t("channels.inviteRevoke")}
            </Button>
          </div>
          {feedback && <p role="status">{t(`channels.${feedback}`)}</p>}
        </div>
      )}
    </div>
  );
}
