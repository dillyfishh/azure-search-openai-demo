import { fetchWithAuthRedirect } from "./api";

export type NotificationType = "info" | "success" | "warning" | "error";

export interface NotificationBanner {
    key: string;
    type: NotificationType;
    title: string | null;
    message: string;
    dismissible: boolean;
}

export interface NotificationsResponse {
    notifications: NotificationBanner[];
    refreshAfterMs: number;
    validForMs: number;
}

export function isSafeNotificationLink(value: string): boolean {
    if (/[\u0000-\u0020\u007f\\]/.test(value)) return false;
    if (value.startsWith("/") && !value.startsWith("//")) return true;
    try {
        const url = new URL(value);
        return url.protocol === "https:" && !!url.hostname && !url.username && !url.password;
    } catch {
        return false;
    }
}

function isBanner(value: unknown): value is NotificationBanner {
    if (!value || typeof value !== "object") return false;
    const banner = value as Record<string, unknown>;
    return (
        typeof banner.key === "string" &&
        ["info", "success", "warning", "error"].includes(banner.type as string) &&
        (banner.title === null || typeof banner.title === "string") &&
        typeof banner.message === "string" &&
        banner.message.length <= 40000 &&
        typeof banner.dismissible === "boolean"
    );
}

export async function notificationsApi(signal: AbortSignal): Promise<NotificationsResponse> {
    const response = await fetchWithAuthRedirect("/notifications", { signal, cache: "no-store" });
    if (!response.ok) throw new Error(`Notifications request failed: ${response.status}`);
    const payload = await response.json();
    if (
        !payload ||
        !Array.isArray(payload.notifications) ||
        payload.notifications.length > 1000 ||
        !payload.notifications.every(isBanner) ||
        !Number.isFinite(payload.refreshAfterMs) ||
        payload.refreshAfterMs < 1 ||
        payload.refreshAfterMs > 3600000 ||
        !Number.isFinite(payload.validForMs) ||
        payload.validForMs < 1 ||
        payload.validForMs > 7200000
    ) {
        throw new Error("Invalid notifications response");
    }
    return payload;
}
