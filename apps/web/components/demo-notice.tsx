"use client";

import { useState } from "react";

import { XIcon } from "@/components/icons";
import { useSession } from "@/lib/session";

/** 演示站提示条「已关闭」的本地记忆 key（设备级，不进后端——演示站本来就不许写） */
const DISMISSED_KEY = "movieclaw.demo-notice-dismissed";

/**
 * 公开演示站（docs/design/demo-site.md）登录后的提示条：告诉访客全站只读、数据
 * 每天还原，免得点了按钮收到拒绝才明白过来。非演示站（session.demo 不为 true）
 * 什么都不渲染。
 *
 * 做成浮在角落的小条而不是占一行的横幅：外壳两套主题、手机与桌面的顶栏 / 底栏
 * 让位都按固定几何写死（lib/themes.ts 的让位契约），插一行会让所有页面错位。
 * 位置避开常驻控件——桌面贴右下角；手机贴在底栏之上（两个主题的底栏几何都由
 * --mobile-tabbar-h / --mobile-tabbar-offset 声明，银玻璃的偏移已含安全区，
 * Netflix 的停靠栏不含，取两者较大值即可同时适配）。Agent 会话页底部是输入框，
 * 外壳在沉浸路由上不挂它。
 *
 * 关了就记在 localStorage 里不再出现；读写失败（隐私模式等）只是记不住，照常可关。
 */
export function DemoNotice() {
  const demo = useSession().session.demo === true;
  // AuthGate 确认登录后才在客户端渲染外壳，初始化时可直接读 localStorage、无水合问题
  const [dismissed, setDismissed] = useState(() => {
    try {
      return localStorage.getItem(DISMISSED_KEY) === "1";
    } catch {
      return false;
    }
  });
  if (!demo || dismissed) return null;

  const dismiss = () => {
    setDismissed(true);
    try {
      localStorage.setItem(DISMISSED_KEY, "1");
    } catch {
      // 写不进去只是下次还会出现，不影响本次关闭
    }
  };

  return (
    <div
      role="status"
      className="fixed z-30 flex items-center gap-1 rounded-2xl border border-white/10 bg-black/70 py-1 pl-3.5 pr-1 text-caption text-white/85 shadow-lg backdrop-blur-md max-md:inset-x-4 max-md:bottom-[calc(max(var(--mobile-tabbar-offset,0px),var(--safe-bottom))+var(--mobile-tabbar-h,54px)+8px)] md:bottom-4 md:right-4 md:max-w-sm"
    >
      <span className="min-w-0 flex-1 py-1">这是 MovieClaw 公开演示站：全站只读，数据每天自动还原。</span>
      <button
        type="button"
        onClick={dismiss}
        aria-label="关闭演示站提示"
        className="flex size-8 shrink-0 items-center justify-center rounded-full text-white/60 transition-colors hover:bg-white/10 hover:text-white max-md:size-9"
      >
        <XIcon className="size-4" />
      </button>
    </div>
  );
}
