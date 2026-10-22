import type { Metadata, Viewport } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Moonwright",
  description:
    "Verified multi-robot construction planning for the lunar surface: a natural-language brief " +
    "is structured by Nemotron, scheduled and verified by OR-Tools CP-SAT, replayed in a 2D " +
    "simulator and stress-tested with Monte Carlo failures.",
};

export const viewport: Viewport = {
  themeColor: "#0b0f14",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    // suppressHydrationWarning: browser extensions (Grammarly, dark readers, ...) add attributes
    // to <html> before React hydrates; the mismatch is theirs, not ours, and the attribute-only
    // suppression is limited to this element.
    <html
      lang="en"
      suppressHydrationWarning
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
    >
      <body className="flex min-h-full flex-col bg-surface font-sans text-fg">{children}</body>
    </html>
  );
}
