"use client";
/**
 * Shared primitives of the mission console: section frames, cards, buttons, badges, fields,
 * notices and stat tiles. Colours come from the tokens in globals.css (one amber accent,
 * semantic ok/fail/warn/info mirrored from lib/palette.ts).
 */
import type { ButtonHTMLAttributes, InputHTMLAttributes, ReactNode } from "react";

export function cx(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(" ");
}

export type Tone = "neutral" | "accent" | "ok" | "fail" | "warn" | "info";

const TONE_CHIP: Record<Tone, string> = {
  neutral: "border-white/10 bg-white/5 text-muted",
  accent: "border-accent/30 bg-accent/10 text-accent",
  ok: "border-ok/30 bg-ok/10 text-ok",
  fail: "border-fail/30 bg-fail/10 text-fail",
  warn: "border-warn/30 bg-warn/10 text-warn",
  info: "border-info/30 bg-info/10 text-info",
};

const TONE_BOX: Record<Tone, string> = {
  neutral: "border-white/10 bg-white/[0.03]",
  accent: "border-accent/30 bg-accent/[0.07]",
  ok: "border-ok/30 bg-ok/[0.07]",
  fail: "border-fail/30 bg-fail/[0.07]",
  warn: "border-warn/30 bg-warn/[0.07]",
  info: "border-info/30 bg-info/[0.07]",
};

const TONE_TEXT: Record<Tone, string> = {
  neutral: "text-fg",
  accent: "text-accent",
  ok: "text-ok",
  fail: "text-fail",
  warn: "text-warn",
  info: "text-info",
};

// ------------------------------------------------------------------ layout
export interface SectionProps {
  id: string;
  /** Step number shown before the title, e.g. "01". */
  step?: string;
  title: string;
  subtitle?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}

export function Section({ id, step, title, subtitle, actions, children, className }: SectionProps) {
  return (
    <section
      id={id}
      aria-labelledby={`${id}-title`}
      className={cx("scroll-mt-24 border-t border-line py-8 first:border-t-0 md:py-10", className)}
    >
      <header className="mb-4 flex flex-wrap items-end justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-baseline gap-2.5">
            {step && (
              <span className="font-mono text-[11px] font-medium tracking-[0.18em] text-accent">
                {step}
              </span>
            )}
            <h2 id={`${id}-title`} className="text-[17px] font-semibold tracking-tight text-fg">
              {title}
            </h2>
          </div>
          {subtitle && <p className="mt-1 max-w-3xl text-[13px] text-muted">{subtitle}</p>}
        </div>
        {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
      </header>
      {children}
    </section>
  );
}

export interface CardProps {
  children: ReactNode;
  className?: string;
  /** Optional header row. */
  title?: ReactNode;
  aside?: ReactNode;
  bodyClassName?: string;
}

export function Card({ children, className, title, aside, bodyClassName = "p-4 md:p-5" }: CardProps) {
  return (
    <div className={cx("rounded-lg border border-line bg-raised", className)}>
      {(title || aside) && (
        <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 border-b border-line px-4 py-2.5">
          <div className="text-[13px] font-medium text-fg">{title}</div>
          {aside && <div className="text-xs text-muted">{aside}</div>}
        </div>
      )}
      <div className={bodyClassName}>{children}</div>
    </div>
  );
}

// ------------------------------------------------------------------ controls
type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";
type ButtonSize = "sm" | "md";

const BUTTON_VARIANT: Record<ButtonVariant, string> = {
  primary: "bg-accent font-semibold text-[#0b0f14] hover:brightness-110 focus-visible:ring-accent/50",
  secondary: "border border-white/15 bg-white/5 text-fg hover:bg-white/10 focus-visible:ring-white/30",
  ghost: "text-muted hover:bg-white/5 hover:text-fg focus-visible:ring-white/30",
  danger: "border border-fail/40 text-fail hover:bg-fail/10 focus-visible:ring-fail/40",
};

const BUTTON_SIZE: Record<ButtonSize, string> = {
  sm: "h-7 gap-1.5 px-2.5 text-xs",
  md: "h-9 gap-2 px-3.5 text-[13px]",
};

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  loading?: boolean;
}

export function Button({
  variant = "secondary",
  size = "md",
  loading = false,
  className,
  children,
  disabled,
  type = "button",
  ...rest
}: ButtonProps) {
  return (
    <button
      type={type}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      className={cx(
        "inline-flex items-center justify-center whitespace-nowrap rounded-md font-medium transition-colors",
        "focus:outline-none focus-visible:ring-2 disabled:cursor-not-allowed disabled:opacity-50",
        BUTTON_VARIANT[variant],
        BUTTON_SIZE[size],
        className,
      )}
      {...rest}
    >
      {loading && <Spinner className="size-3.5" />}
      {children}
    </button>
  );
}

export const inputClass =
  "h-8 w-full min-w-0 rounded-md border border-white/10 bg-surface px-2.5 text-[13px] text-fg " +
  "placeholder:text-dim focus:border-accent/60 focus:outline-none focus:ring-2 focus:ring-accent/20 " +
  "disabled:opacity-50 aria-invalid:border-fail/60";

export interface TextInputProps extends InputHTMLAttributes<HTMLInputElement> {
  invalid?: boolean;
}

export function TextInput({ invalid, className, ...rest }: TextInputProps) {
  return <input aria-invalid={invalid || undefined} className={cx(inputClass, className)} {...rest} />;
}

export interface NumberInputProps
  extends Omit<InputHTMLAttributes<HTMLInputElement>, "value" | "onChange" | "type"> {
  /** NaN means "empty / not a number"; validation treats it as invalid. */
  value: number;
  onChange: (value: number) => void;
  invalid?: boolean;
}

/** Controlled numeric input; React keeps the in-progress text ("12.") while the value is NaN. */
export function NumberInput({ value, onChange, invalid, className, ...rest }: NumberInputProps) {
  return (
    <input
      type="number"
      inputMode="decimal"
      aria-invalid={invalid || undefined}
      className={cx(inputClass, "font-mono tabular-nums", className)}
      value={Number.isFinite(value) ? value : ""}
      onChange={(e) => onChange(e.target.value === "" ? NaN : Number(e.target.value))}
      {...rest}
    />
  );
}

export interface FieldProps {
  label: ReactNode;
  hint?: ReactNode;
  error?: ReactNode;
  children: ReactNode;
  className?: string;
  /** "div" for a group of controls; the default label element focuses its single control. */
  as?: "label" | "div";
}

export function Field({ label, hint, error, children, className, as = "label" }: FieldProps) {
  const Tag = as;
  return (
    <Tag className={cx("block min-w-0", className)}>
      <span className="mb-1 block text-[11px] font-medium uppercase tracking-wider text-dim">
        {label}
      </span>
      {children}
      {error ? (
        <span className="mt-1 block text-[11px] text-fail">{error}</span>
      ) : hint ? (
        <span className="mt-1 block text-[11px] text-dim">{hint}</span>
      ) : null}
    </Tag>
  );
}

// ------------------------------------------------------------------ feedback
export interface BadgeProps {
  tone?: Tone;
  mono?: boolean;
  className?: string;
  title?: string;
  children: ReactNode;
}

export function Badge({ tone = "neutral", mono, className, title, children }: BadgeProps) {
  return (
    <span
      title={title}
      className={cx(
        "inline-flex max-w-full items-center gap-1 rounded border px-1.5 py-px text-[11px] leading-[18px]",
        mono && "font-mono",
        TONE_CHIP[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}

export interface NoticeProps {
  tone?: Tone;
  title?: ReactNode;
  children?: ReactNode;
  className?: string;
}

export function Notice({ tone = "info", title, children, className }: NoticeProps) {
  return (
    <div
      role={tone === "fail" ? "alert" : "status"}
      className={cx("rounded-md border px-3.5 py-2.5 text-[13px]", TONE_BOX[tone], className)}
    >
      {title && <div className={cx("font-medium", TONE_TEXT[tone])}>{title}</div>}
      {children && <div className={cx(title ? "mt-0.5" : null, "text-muted")}>{children}</div>}
    </div>
  );
}

export interface StatProps {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  /** The one big number of a view. */
  hero?: boolean;
  tone?: Tone;
}

export function Stat({ label, value, sub, hero, tone = "neutral" }: StatProps) {
  return (
    <div className="min-w-0">
      <div className="text-[11px] font-medium uppercase tracking-wider text-dim">{label}</div>
      <div
        className={cx(
          "mt-0.5 font-semibold tracking-tight",
          hero ? "text-4xl md:text-5xl" : "text-xl tabular-nums",
          TONE_TEXT[tone],
        )}
      >
        {value}
      </div>
      {sub && <div className="mt-0.5 text-xs text-muted">{sub}</div>}
    </div>
  );
}

export function Dot({ color, className, pulse }: { color: string; className?: string; pulse?: boolean }) {
  return (
    <span
      aria-hidden="true"
      className={cx("inline-block size-2 shrink-0 rounded-full", pulse && "mw-pulse", className)}
      style={{ backgroundColor: color }}
    />
  );
}

export function Spinner({ className }: { className?: string }) {
  return (
    <svg
      className={cx("animate-spin", className ?? "size-4")}
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden="true"
    >
      <circle cx="12" cy="12" r="9" stroke="currentColor" strokeOpacity="0.25" strokeWidth="3" />
      <path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
    </svg>
  );
}

export function IconCheck({ className }: { className?: string }) {
  return (
    <svg className={className ?? "size-3"} viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path d="M3 8.5l3 3 7-7" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function IconX({ className }: { className?: string }) {
  return (
    <svg className={className ?? "size-3"} viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" />
    </svg>
  );
}
