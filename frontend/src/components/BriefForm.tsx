"use client";
import type { KeyboardEvent } from "react";
import type { ExampleBrief } from "@/lib/types";
import { Badge, Button, Card, Notice, Section, Spinner, cx } from "./Section";

export interface BriefFormProps {
  brief: string;
  /** Example id sent as brief_id (offline fixtures are keyed by it); null for a free brief. */
  briefId: string | null;
  onBriefChange: (text: string) => void;
  examples: ExampleBrief[];
  examplesLoading: boolean;
  /** Shown next to the chips, e.g. when the examples came from the bundled samples. */
  examplesNote: string | null;
  onUseExample: (example: ExampleBrief) => void;
  onStructure: () => void;
  structuring: boolean;
  error: string | null;
  disabled?: boolean;
}

const PLACEHOLDER = `Site. LS-1 plateau near the lunar south pole, 80 m by 80 m of loose regolith; depot on the south edge; no recharging.
Structures. 1. Landing pad: circular, 20 m diameter, excavate 0.4 m, compact, sinter a 30 cm surface. 2. Blast berm: 60 m arc, 2.5 m high, built from pad spoil.
Fleet. EX1 and EX2 (excavators, 5000 Wh), HL1 (hauler, 3000 Wh), CP1 (compactor, 3500 Wh), PR1 (printer, 4000 Wh), SV1 (surveyor, 1500 Wh).
Deadline. 14 days. Only one robot per zone at a time.`;

const chipBase =
  "max-w-full truncate rounded-full border px-3 py-1 text-xs transition-colors disabled:cursor-not-allowed disabled:opacity-50";
const chipIdle = "border-white/10 bg-white/5 text-muted hover:border-white/20 hover:text-fg";
const chipActive = "border-accent/60 bg-accent/10 text-accent";

export function BriefForm({
  brief,
  briefId,
  onBriefChange,
  examples,
  examplesLoading,
  examplesNote,
  onUseExample,
  onStructure,
  structuring,
  error,
  disabled = false,
}: BriefFormProps) {
  const canStructure = brief.trim().length > 0 && !structuring && !disabled;
  const words = brief.trim() ? brief.trim().split(/\s+/).length : 0;

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter" && canStructure) {
      e.preventDefault();
      onStructure();
    }
  };

  return (
    <Section
      id="brief"
      step="01"
      title="Brief"
      subtitle="Describe the site, the structures, the robot fleet and the deadline in plain English. Nemotron turns it into structured requirements that you can correct before anything is scheduled."
    >
      <Card>
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <span className="text-xs text-dim">Examples</span>
          {examplesLoading && <Spinner className="size-3 text-dim" />}
          {examples.map((ex) => (
            <button
              key={ex.id}
              type="button"
              onClick={() => onUseExample(ex)}
              disabled={disabled || structuring}
              aria-pressed={briefId === ex.id}
              className={cx(chipBase, briefId === ex.id ? chipActive : chipIdle)}
              title={ex.id}
            >
              {ex.title}
            </button>
          ))}
          {!examplesLoading && examples.length === 0 && (
            <span className="text-xs text-dim">no examples available</span>
          )}
          {examplesNote && <span className="text-[11px] text-dim">{examplesNote}</span>}
        </div>

        <textarea
          id="brief-text"
          aria-label="Construction brief"
          value={brief}
          onChange={(e) => onBriefChange(e.target.value)}
          onKeyDown={onKeyDown}
          disabled={disabled}
          rows={10}
          spellCheck={false}
          placeholder={PLACEHOLDER}
          className="min-h-[220px] w-full resize-y rounded-md border border-white/10 bg-surface p-3 text-[13.5px] leading-relaxed text-fg placeholder:text-dim/80 focus:border-accent/60 focus:outline-none focus:ring-2 focus:ring-accent/20 disabled:opacity-60"
        />

        <div className="mt-3 flex flex-wrap items-center justify-between gap-3">
          <div className="flex flex-wrap items-center gap-2 text-xs text-dim">
            <span className="font-mono tabular-nums">{words} words</span>
            {briefId && (
              <Badge
                tone="accent"
                mono
                title="Sent as brief_id with every request. Offline fixtures replay this example even if you edit the text; clear the textarea to detach."
              >
                brief_id: {briefId}
              </Badge>
            )}
            <span className="hidden md:inline">Cmd/Ctrl + Enter to structure</span>
          </div>
          <Button variant="primary" onClick={onStructure} disabled={!canStructure} loading={structuring}>
            {structuring ? "Structuring…" : "Structure brief"}
          </Button>
        </div>

        {error && (
          <Notice tone="fail" title="Structuring failed" className="mt-3">
            {error}
          </Notice>
        )}
      </Card>
    </Section>
  );
}
