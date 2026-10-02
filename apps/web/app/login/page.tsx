"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import type { Route } from "next";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";

import { CheckIcon, ChevronDownIcon, LockIcon, UserIcon } from "@/components/icons";
import { CopyButton } from "@/components/copy-button";
import {
  WelcomeCard,
  WelcomeError,
  WelcomeField,
  WelcomeFields,
  WelcomeScreen,
  WelcomeSubmit,
} from "@/components/welcome-screen";
import { getBootstrapStatus, getSession, login } from "@/lib/api/auth";
import { reloadAfterAccountChange } from "@/lib/account-reload";
import { usePageTitle } from "@/lib/use-page-title";
import { HttpError } from "@/lib/http";
import { accessiblePathFor } from "@/lib/permissions";
import type { DemoAccount, DemoSite, SessionView } from "@/lib/api/auth";

/**
 * 登录成功 / 已登录后要跳回的目标地址：取自 ?next= 参数（会话过期时由 http.ts 写入）。
 * 只接受站内相对路径（以单个 / 开头），拒绝 //host、http(s):// 等外站地址，防开放重定向；
 * 缺失或非法时回落到首页。
 */
function resolveNext(session?: SessionView): string {
  if (typeof window === "undefined") return "/";
  // 公开演示站先落在媒体库：访客第一眼看到海报墙，AI 助手在侧栏里
  const home = session?.demo ? "/library" : "/";
  const raw = new URLSearchParams(window.location.search).get("next");
  if (!raw) return session ? accessiblePathFor(session, home) : "/";
  const next = decodeURIComponent(raw);
  if (next.startsWith("/") && !next.startsWith("//")) {
    return session ? accessiblePathFor(session, next) : next;
  }
  return session ? accessiblePathFor(session, home) : "/";
}

/** 是否处于"添加账号"形态（用户菜单里点「添加账号」带 ?add=1 进来）。 */
function isAddingAccount(): boolean {
  if (typeof window === "undefined") return false;
  return new URLSearchParams(window.location.search).get("add") === "1";
}

/** 是否是会话过期被送回来的（http.ts 写入 ?next=）：直接给登录卡片，不先停在首页 */
function isReturning(): boolean {
  if (typeof window === "undefined") return false;
  return new URLSearchParams(window.location.search).has("next");
}

/**
 * 登录页：星空欢迎页（components/welcome-screen.tsx，对齐原生 App 的欢迎页），
 * 首页底部「登录」按钮升起登录卡片。网页不需要服务器地址——页面本身就是从服务器打开的。
 *
 * 挂载时做两个跳转判断：
 * 1. 系统尚未初始化 → 转 /setup 引导页（首次部署的入口）；
 * 2. 已持有效会话 → 直接回 next 目标（默认首页），不重复登录。
 * 判断完之前不放片头，免得已登录的人先看到星空再被跳走。
 * 安全性完全由后端保证，这里的跳转只是导航体验。
 *
 * 一进来就是卡片的两种情况：会话过期回来（?next=）与「添加账号」（?add=1，
 * docs/design/account-switching.md §4：已登录也不跳走，登录成功后后端自动把新账号
 * 并入本浏览器的账号列表）。添加账号的卡片 × 回到当前账号。
 */
export default function LoginPage() {
  // 挂载后再读 URL：服务端渲染没有 window，初值若按 URL 算会造成水合不一致
  const [adding, setAdding] = useState(false);
  const [returning, setReturning] = useState(false);
  const [ready, setReady] = useState(false);
  // 公开演示站（docs/design/demo-site.md）：身份菜单提供可一键填入的演示账号
  const [demo, setDemo] = useState<DemoSite | null>(null);
  useEffect(() => {
    setAdding(isAddingAccount());
    setReturning(isReturning());
  }, []);
  usePageTitle(adding ? "添加账号" : "登录");
  const router = useRouter();

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const status = await getBootstrapStatus();
        if (cancelled) return;
        setDemo(status.demo ?? null);
        if (!status.initialized) {
          router.replace("/setup");
          return;
        }
        // 添加账号：已登录也留在本页。直接读 URL 而不用 adding 状态——
        // 状态要等首个 effect 才更新，这里不能抢在它前面把人跳走
        if (isAddingAccount()) {
          setReady(true);
          return;
        }
        const session = await getSession(); // 已登录则不抛错
        if (!cancelled) router.replace(resolveNext(session) as Route);
      } catch {
        // 未登录（401）或后端暂不可达：留在登录页
        if (!cancelled) setReady(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [router]);

  return (
    <WelcomeScreen
      buttonLabel="登录"
      ready={ready}
      initialStage={adding || returning ? "card" : "home"}
      card={({ close, autoFocus }) => (
        <LoginCard
          adding={adding}
          autoFocus={autoFocus}
          demo={demo}
          onClose={adding ? () => router.replace("/") : close}
        />
      )}
    />
  );
}

function LoginCard({
  adding,
  autoFocus,
  demo,
  onClose,
}: {
  adding: boolean;
  autoFocus: boolean;
  demo: DemoSite | null;
  onClose: () => void;
}) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [remember, setRemember] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const usernameRef = useRef<HTMLInputElement>(null);

  // 用户点按钮打开的卡片：等卡片升起后聚焦用户名（同 App：动画与弹键盘挤在一起会卡）；
  // 页面自己出现的卡片只在有物理键盘的设备上聚焦，手机上不抢着弹键盘
  useEffect(() => {
    if (!autoFocus && !window.matchMedia("(pointer: fine)").matches) return;
    const timer = window.setTimeout(() => usernameRef.current?.focus(), autoFocus ? 450 : 0);
    return () => window.clearTimeout(timer);
  }, [autoFocus]);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (busy) return;
    setError(null);
    setBusy(true);
    try {
      const session = await login(username.trim(), password, remember);
      // 整页跳转而非路由跳转：让 AppShell 及全部数据在已登录态下重新初始化。
      // 回到 next 指向的页面（会话过期前所在处），默认首页；跳之前先备好新账号的
      // 壁纸与界面偏好首帧缓存，进工作台不闪默认图（lib/account-reload.ts）
      await reloadAfterAccountChange(resolveNext(session), true);
    } catch (err) {
      setError(err instanceof HttpError ? err.message : "网络异常，请稍后重试");
      setBusy(false);
    }
  };

  return (
    <WelcomeCard
      title={adding ? "添加账号" : "登录 MovieClaw"}
      subtitle={
        adding
          ? "登录另一个账号；之后可在用户菜单里一键切换，不用再输密码。"
          : demo
            ? demo.notice || "这是公开演示站，选择演示身份即可填入账号。"
            : "使用你在这台服务器上的账号进入。"
      }
      onClose={onClose}
    >
      <form onSubmit={submit} className="space-y-4">
        {demo && demo.accounts.length > 0 && (
          <DemoAccountPicker
            accounts={demo.accounts}
            selected={username.trim()}
            disabled={busy}
            onPick={(account) => {
              setUsername(account.username);
              setPassword(account.password);
              setError(null);
            }}
          />
        )}
        <WelcomeFields>
          <WelcomeField
            ref={usernameRef}
            icon={<UserIcon className="size-[18px]" />}
            placeholder="用户名"
            type="text"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoComplete="username"
            autoCapitalize="none"
            autoCorrect="off"
            spellCheck={false}
            enterKeyHint="next"
          />
          <WelcomeField
            icon={<LockIcon className="size-[18px]" />}
            placeholder="密码"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
            enterKeyHint="go"
          />
        </WelcomeFields>
        <label className="flex cursor-pointer items-center gap-2 px-1 text-sub text-[var(--text-muted)]">
          <input
            type="checkbox"
            checked={remember}
            onChange={(e) => setRemember(e.target.checked)}
            className="size-3.5 accent-[var(--accent-strong)]"
          />
          30 天内记住我
        </label>
        <WelcomeError message={error} />
        <WelcomeSubmit busy={busy} disabled={busy || !username.trim() || !password}>
          {busy ? "正在登录…" : adding ? "添加并切换" : "登录"}
        </WelcomeSubmit>
      </form>
    </WelcomeCard>
  );
}

/**
 * 演示身份选择：折叠菜单让小屏登录卡片保持紧凑，展开时说明各角色的可见范围。
 * 选择只填表单、不自动登录；App 登录信息常驻，公开密码不必等选身份后才显示。
 * 复用复制按钮的剪贴板兼容路径，照顾 iOS 和未开放 Clipboard API 的浏览器。
 * 复用 Radix 的单选菜单处理焦点、键盘与弹层避让，不额外实现一套下拉交互。
 */
function DemoAccountPicker({
  accounts,
  selected,
  disabled,
  onPick,
}: {
  accounts: DemoAccount[];
  selected: string;
  disabled: boolean;
  onPick: (account: DemoAccount) => void;
}) {
  const current = accounts.find((account) => account.username === selected);
  // 只提示 bootstrap 公开的凭据；未选身份时，仅在各账号密码一致时提前展示。
  const publicPassword = current?.password ?? (
    accounts.every((account) => account.password === accounts[0].password) ? accounts[0].password : null
  );
  const [serverAddress, setServerAddress] = useState("");
  useEffect(() => setServerAddress(window.location.origin), []);
  return (
    <div>
      <DropdownMenu.Root>
        <DropdownMenu.Trigger asChild>
          <button
            type="button"
            disabled={disabled}
            aria-label="选择演示身份"
            className="flex min-h-[50px] w-full items-center gap-3 rounded-2xl border border-white/[0.12] bg-white/[0.06] px-3.5 text-left transition-colors hover:bg-white/[0.10] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--accent-strong)] disabled:opacity-40"
          >
            <UserIcon aria-hidden="true" className="size-[18px] shrink-0 text-[var(--text-muted)]" />
            <span className="flex-1 text-body text-[var(--text)]">
              {current?.label ?? "选择演示身份"}
            </span>
            {current && <span className="text-sub text-[var(--text-muted)]">切换</span>}
            <ChevronDownIcon aria-hidden="true" className="size-4 text-[var(--text-muted)]" />
          </button>
        </DropdownMenu.Trigger>
        <DropdownMenu.Portal>
          <DropdownMenu.Content
            align="start"
            sideOffset={8}
            collisionPadding={16}
            className="menu-surface z-[100] max-h-[var(--radix-dropdown-menu-content-available-height)] w-[var(--radix-dropdown-menu-trigger-width)] overflow-y-auto rounded-2xl p-1.5"
          >
            <DropdownMenu.RadioGroup
              value={current?.username ?? ""}
              onValueChange={(value) => {
                const account = accounts.find((item) => item.username === value);
                if (account) onPick(account);
              }}
            >
              {accounts.map((account) => (
                <DropdownMenu.RadioItem
                  key={account.username}
                  value={account.username}
                  className="flex cursor-pointer items-center gap-3 rounded-xl px-3 py-2.5 outline-none data-[highlighted]:bg-white/[0.08] data-[state=checked]:bg-white/[0.05]"
                >
                  <span className="min-w-0 flex-1">
                    <span className="block text-body text-[var(--text)]">{account.label}</span>
                    {account.description && (
                      <span className="mt-0.5 line-clamp-2 text-sub leading-snug text-[var(--text-muted)]">
                        {account.description}
                      </span>
                    )}
                  </span>
                  <span className="size-4 shrink-0 text-[var(--text)]">
                    <DropdownMenu.ItemIndicator>
                      <CheckIcon aria-hidden="true" className="size-4" />
                    </DropdownMenu.ItemIndicator>
                  </span>
                </DropdownMenu.RadioItem>
              ))}
            </DropdownMenu.RadioGroup>
          </DropdownMenu.Content>
        </DropdownMenu.Portal>
      </DropdownMenu.Root>
      <section aria-label="iOS / App 登录信息" className="mt-3 rounded-2xl border border-white/[0.08] bg-white/[0.04] px-3">
        <h3 className="pt-2.5 text-caption text-[var(--text-muted)]">iOS / App 登录信息</h3>
        <dl aria-live="polite" className="divide-y divide-white/[0.06]">
          {[
            { label: "账号", value: current?.username },
            { label: "密码", value: publicPassword },
            { label: "服务器地址", value: serverAddress },
          ].map(({ label, value }) => (
            <div key={label} className="flex min-h-[44px] items-center gap-2 py-1">
              <div className="min-w-0 flex-1">
                <dt className="text-caption text-[var(--text-muted)]">{label}</dt>
                <dd className="select-text break-all font-mono text-sub text-[var(--text)]">
                  {value || (label === "账号" ? "选择上方演示身份" : "选择身份后显示")}
                </dd>
              </div>
              {value && (
                <CopyButton
                  key={value}
                  text={value}
                  label="复制"
                  ariaLabel={`复制${label}`}
                  className="min-h-[44px] shrink-0 px-2 text-caption text-[var(--text-muted)] hover:text-[var(--text)] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--accent-strong)]"
                />
              )}
            </div>
          ))}
        </dl>
      </section>
    </div>
  );
}
