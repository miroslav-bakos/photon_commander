import { SetupLink } from "../Setup/SetupTab";
import { BugReportPanel } from "./BugReportPanel";
// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { translate, useLanguage } from "../../i18n/locale";
import { LocalizedText } from "../../i18n/jsx-runtime";
import { Card } from "../../components/Card";
import { guideTopics, knowledgeArticles, manualTopics } from "./helpContent";

type HelpSection = "guide" | "manual" | "knowledge";

const SECTION_LABELS: Record<HelpSection, string> = {
  guide: "Guide",
  manual: "Manual",
  knowledge: "Knowledge base",
};

function SectionTabs({ active, onChange }: { active: HelpSection; onChange: (section: HelpSection) => void }) {
  return (
    <div role="tablist" aria-label="Help sections" className="grid grid-cols-3 gap-1 rounded-lg border border-border bg-surface p-1">
      {(Object.keys(SECTION_LABELS) as HelpSection[]).map((section) => (
        <button
          key={section}
          type="button"
          role="tab"
          aria-selected={active === section}
          onClick={() => { onChange(section); }}
          className={`min-h-11 rounded-md px-2 text-sm font-medium ${active === section ? "bg-accent-muted text-accent" : "text-fg-muted hover:text-fg"}`}
        >
          {SECTION_LABELS[section]}
        </button>
      ))}
    </div>
  );
}

function RelatedManualLinks({ ids, openManual }: { ids: string[]; openManual: (id: string) => void }) {
  return (
    <div className="mt-4 flex flex-wrap items-center gap-2">
      <span className="text-xs font-medium tracking-wide text-fg-muted uppercase">Related manual</span>
      {ids.map((id) => {
        const topic = manualTopics.find((candidate) => candidate.id === id);
        return topic === undefined ? null : (
          <button key={id} type="button" onClick={() => { openManual(id); }} className="min-h-11 rounded-md px-3 text-sm text-link underline">
            {topic.title}
          </button>
        );
      })}
    </div>
  );
}

function Guide({ openManual }: { openManual: (id: string) => void }) {
  return (
    <div role="tabpanel" aria-label="Guide" className="space-y-4">
      <div>
        <h2 className="text-xl font-semibold">Task guides</h2>
        <p className="mt-1 text-sm text-fg-muted">Start with a goal. Each guide links to the detailed screen reference you may need.</p>
      </div>
      {guideTopics.map((guide) => (
        <Card key={guide.id} id={`guide-${guide.id}`}>
          <h3 className="text-base font-semibold">{guide.title}</h3>
          <p className="mt-1 text-sm text-fg-muted">{guide.summary}</p>
          <ol className="mt-4 list-decimal space-y-2 pl-5 text-sm">
            {guide.steps.map((step) => <li key={step} className="pl-1">{step}</li>)}
          </ol>
          <RelatedManualLinks ids={guide.relatedManualIds} openManual={openManual} />
        </Card>
      ))}
    </div>
  );
}

function Manual({ selectedId, onSelect }: { selectedId: string; onSelect: (id: string) => void }) {
  const selected = manualTopics.find((topic) => topic.id === selectedId) ?? manualTopics[0];
  if (selected === undefined) return null;

  return (
    <div role="tabpanel" aria-label="Manual" className="grid gap-4 lg:grid-cols-[14rem_minmax(0,1fr)]">
      <nav aria-label="Manual screens" className="lg:sticky lg:top-0 lg:self-start">
        <p className="mb-2 text-xs font-medium tracking-wide text-fg-muted uppercase">Screen reference</p>
        <div className="grid grid-cols-2 gap-1 sm:grid-cols-4 lg:grid-cols-1">
          {manualTopics.map((topic) => (
            <button
              key={topic.id}
              type="button"
              aria-current={selected.id === topic.id ? "page" : undefined}
              onClick={() => { onSelect(topic.id); }}
              className={`min-h-11 rounded-md px-3 text-left text-sm font-medium ${selected.id === topic.id ? "bg-accent-muted text-accent" : "text-fg-muted hover:bg-surface hover:text-fg"}`}
            >
              {topic.title}
            </button>
          ))}
        </div>
      </nav>
      <Card key={selected.id} id={`manual-${selected.id}`}>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p className="text-xs font-medium tracking-wide text-fg-muted uppercase">Screen manual</p>
            <h2 className="mt-1 text-xl font-semibold">{selected.title}</h2>
          </div>
          <Link to={selected.route} className="inline-flex min-h-11 items-center rounded-md px-3 text-sm font-medium text-link underline">
            <LocalizedText value="Open" /> <LocalizedText value={selected.title} />
          </Link>
        </div>
        <p className="mt-2 text-fg-muted">{selected.summary}</p>
        <h3 className="mt-6 font-semibold">What it can do</h3>
        <ul className="mt-2 list-disc space-y-2 pl-5 text-sm">
          {selected.capabilities.map((capability) => <li key={capability} className="pl-1">{capability}</li>)}
        </ul>
        <h3 className="mt-6 font-semibold">How to use it</h3>
        <ol className="mt-2 list-decimal space-y-2 pl-5 text-sm">
          {selected.steps.map((step) => <li key={step} className="pl-1">{step}</li>)}
        </ol>
        <div className="mt-6 rounded-md border border-border bg-surface-raised p-3">
          <h3 className="font-semibold">Know before you start</h3>
          <ul className="mt-2 list-disc space-y-2 pl-5 text-sm text-fg-muted">
            {selected.notes.map((note) => <li key={note} className="pl-1">{note}</li>)}
          </ul>
        </div>
      </Card>
    </div>
  );
}

function KnowledgeBase({ openManual }: { openManual: (id: string) => void }) {
  const [query, setQuery] = useState("");
  const locale = useLanguage();
  const normalizedQuery = query.trim().toLocaleLowerCase();
  const matches = useMemo(() => knowledgeArticles.filter((article) => {
    if (normalizedQuery === "") return true;
    return [article.title, article.category, ...article.body, ...article.keywords]
      .flatMap((text) => [text, translate(text, locale)])
      .join(" ")
      .toLocaleLowerCase()
      .includes(normalizedQuery);
  }), [normalizedQuery, locale]);

  return (
    <div role="tabpanel" aria-label="Knowledge base" className="space-y-4">
      <div>
        <h2 className="text-xl font-semibold">Knowledge base</h2>
        <p className="mt-1 text-sm text-fg-muted">Search concepts, safety guidance, and common problems in plain language.</p>
      </div>
      <label className="grid gap-1 text-sm font-medium">
        Search the knowledge base
        <input
          type="search"
          value={query}
          onChange={(event) => { setQuery(event.target.value); }}
          placeholder="Try “plate solve”, “wrong time”, or “stack”"
          className="min-h-11 rounded-md border border-border bg-bg px-3 text-fg placeholder:text-fg-muted"
        />
      </label>
      <p className="text-xs text-fg-muted" role="status"><LocalizedText value={`${String(matches.length)} article${matches.length === 1 ? "" : "s"}`} /></p>
      {matches.length === 0 && (
        <Card><p className="text-sm text-fg-muted">No matching article. Try a shorter term or browse the Manual by screen.</p></Card>
      )}
      {matches.map((article) => (
        <Card key={article.id} id={`knowledge-${article.id}`}>
          <p className="text-xs font-medium tracking-wide text-fg-muted uppercase">{article.category}</p>
          <h3 className="mt-1 text-base font-semibold">{article.title}</h3>
          <div className="mt-3 space-y-2 text-sm text-fg-muted">
            {article.body.map((paragraph) => <p key={paragraph}>{paragraph}</p>)}
          </div>
          <RelatedManualLinks ids={article.relatedManualIds} openManual={openManual} />
        </Card>
      ))}
    </div>
  );
}

export function HelpTab() {
  const [searchParams] = useSearchParams();
  const requestedSection = searchParams.get("section");
  const [section, setSection] = useState<HelpSection>(
    requestedSection === "manual" || requestedSection === "knowledge" ? requestedSection : "guide",
  );
  const [manualId, setManualId] = useState(manualTopics[0]?.id ?? "planning");

  function openManual(id: string): void {
    setManualId(id);
    setSection("manual");
  }

  return (
    <div className="mx-auto w-full max-w-5xl space-y-5">
      <header>
        <h1 className="text-2xl font-semibold">Help center</h1>
        <p className="mt-1 max-w-3xl text-sm text-fg-muted">
          Learn a complete workflow in the Guide, look up every screen in the Manual, or solve a specific problem in the Knowledge base.
        </p>
      </header>
      <SetupLink />
      <BugReportPanel />
      <SectionTabs active={section} onChange={setSection} />
      {section === "guide" && <Guide openManual={openManual} />}
      {section === "manual" && <Manual selectedId={manualId} onSelect={setManualId} />}
      {section === "knowledge" && <KnowledgeBase openManual={openManual} />}
    </div>
  );
}
