import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import { useTranslation } from "react-i18next";
import { NotificationBanner, NotificationType, isSafeNotificationLink, notificationsApi } from "../../api/notifications";
import styles from "./NotificationBanners.module.css";

const storageKey = "notification-banner-dismissals";
const icons: Record<NotificationType, string> = { info: "ⓘ", success: "✓", warning: "⚠", error: "!" };
const keyOf = (banner: NotificationBanner) => banner.key;

function loadDismissals(): string[] {
    try {
        const saved: unknown = JSON.parse(sessionStorage.getItem(storageKey) ?? "[]");
        return Array.isArray(saved) ? saved.filter((item): item is string => typeof item === "string").slice(-1000) : [];
    } catch {
        return [];
    }
}

/** Reusable presentation component; callers can supply their own data source. */
export function NotificationBannerList({
    notifications,
    onDismiss,
    className
}: {
    notifications: NotificationBanner[];
    onDismiss?: (notification: NotificationBanner) => void;
    className?: string;
}) {
    const { t } = useTranslation();
    if (!notifications.length) return null;
    return (
        <section aria-label={t("notifications.region")} className={`${styles.banners} ${className ?? ""}`}>
            {notifications.map(banner => (
                <div key={keyOf(banner)} className={`${styles.banner} ${styles[banner.type]}`} role={banner.type === "error" ? "alert" : "status"}>
                    <span className={styles.icon} aria-hidden="true">
                        {icons[banner.type]}
                    </span>
                    <div className={styles.content}>
                        <span className={styles.srOnly}>{t(`notifications.${banner.type}`)}: </span>
                        {banner.title && <strong className={styles.title}>{banner.title}</strong>}
                        <div className={styles.message}>
                            <ReactMarkdown
                                skipHtml
                                disallowedElements={["img"]}
                                urlTransform={url => (isSafeNotificationLink(url) ? url : undefined)}
                                components={{ a: ({ href, children }) => (href ? <a href={href}>{children}</a> : <span>{children}</span>) }}
                            >
                                {banner.message}
                            </ReactMarkdown>
                        </div>
                    </div>
                    {banner.dismissible && onDismiss && (
                        <button
                            type="button"
                            className={styles.dismiss}
                            aria-label={t("notifications.dismiss", { title: banner.title ?? t(`notifications.${banner.type}`) })}
                            onClick={() => onDismiss(banner)}
                        >
                            ×
                        </button>
                    )}
                </div>
            ))}
        </section>
    );
}

/** Mount anywhere in the app. Source settings and refresh cadence come from the backend. */
export function NotificationBanners({ className }: { className?: string }) {
    const [notifications, setNotifications] = useState<NotificationBanner[]>([]);
    const [dismissals, setDismissals] = useState(loadDismissals);

    useEffect(() => {
        let disposed = false;
        let pollTimer: ReturnType<typeof setTimeout>;
        let expiryTimer: ReturnType<typeof setTimeout>;
        let controller: AbortController;
        let retryMs = 60000;
        let generation = 0;

        const refresh = async () => {
            const requestGeneration = ++generation;
            clearTimeout(pollTimer);
            controller?.abort();
            const requestController = new AbortController();
            controller = requestController;
            const started = performance.now();
            // Keep a hung request from preventing all future refreshes.
            const timeout = setTimeout(() => requestController.abort(), 10000);
            try {
                const payload = await notificationsApi(requestController.signal);
                if (disposed || requestGeneration !== generation) return;
                const elapsed = performance.now() - started;
                const remaining = Math.max(0, payload.validForMs - elapsed);
                retryMs = Math.max(1000, payload.refreshAfterMs);
                setNotifications(remaining > 0 ? payload.notifications : []);
                clearTimeout(expiryTimer);
                expiryTimer = setTimeout(() => setNotifications([]), remaining);
                pollTimer = setTimeout(refresh, Math.max(50, payload.refreshAfterMs - elapsed));
            } catch {
                if (disposed || requestGeneration !== generation) return;
                clearTimeout(expiryTimer);
                setNotifications([]);
                pollTimer = setTimeout(refresh, retryMs);
            } finally {
                clearTimeout(timeout);
            }
        };
        const resume = () => {
            if (document.visibilityState === "visible") {
                // Timers may have been suspended; do not redisplay an old snapshot.
                clearTimeout(expiryTimer);
                setNotifications([]);
                void refresh();
            }
        };
        void refresh();
        document.addEventListener("visibilitychange", resume);
        window.addEventListener("online", resume);
        return () => {
            disposed = true;
            generation++;
            controller?.abort();
            clearTimeout(pollTimer);
            clearTimeout(expiryTimer);
            document.removeEventListener("visibilitychange", resume);
            window.removeEventListener("online", resume);
        };
    }, []);

    const dismiss = (banner: NotificationBanner) => {
        setDismissals(previous => {
            const updated = [...new Set([...previous, keyOf(banner)])].slice(-1000);
            try {
                sessionStorage.setItem(storageKey, JSON.stringify(updated));
            } catch {
                // Storage may be disabled; in-memory dismissal still works.
            }
            return updated;
        });
    };

    return (
        <NotificationBannerList
            className={className}
            notifications={notifications.filter(banner => !banner.dismissible || !dismissals.includes(keyOf(banner)))}
            onDismiss={dismiss}
        />
    );
}
