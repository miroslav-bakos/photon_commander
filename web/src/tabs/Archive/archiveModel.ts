// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import type { ArchiveEntry } from "../../api/capture";

export const SESSION_GAP_MS = 30 * 60_000;

export interface ArchiveSession {
  id: string;
  target: string;
  day: string;
  startedAt: string;
  endedAt: string;
  entries: ArchiveEntry[];
}

export interface ArchiveFolder {
  key: string;
  name: string;
  parent: string | null;
  depth: number;
  directFiles: number;
  totalFiles: number;
  totalBytes: number;
}

export interface CalendarCell {
  day: string;
  dayOfMonth: number;
  inMonth: boolean;
  fileCount: number;
  sessionCount: number;
}

export function observedDay(entry: ArchiveEntry): string {
  return entry.date_obs.slice(0, 10);
}

export function isCalibration(entry: ArchiveEntry): boolean {
  const frameType = entry.frame_type?.toLocaleLowerCase();
  const filter = entry.filter_name?.toLocaleLowerCase();
  return frameType === "dark" || frameType === "flat" || frameType === "bias"
    || filter === "dark" || entry.directory.toLocaleLowerCase().includes("calibration");
}

function sessionTarget(entry: ArchiveEntry): string {
  if (isCalibration(entry)) return `${entry.frame_type?.toLocaleLowerCase() ?? "calibration"} calibration`;
  const target = entry.object_name?.trim();
  return target === undefined || target.length === 0 ? "Untitled session" : target;
}

export function deriveArchiveSessions(entries: ArchiveEntry[]): ArchiveSession[] {
  const byTarget = new Map<string, ArchiveEntry[]>();
  for (const entry of entries) {
    const target = sessionTarget(entry);
    byTarget.set(target, [...(byTarget.get(target) ?? []), entry]);
  }
  const sessions: ArchiveSession[] = [];
  for (const [target, targetEntries] of byTarget) {
    const sorted = [...targetEntries].sort((a, b) => a.date_obs.localeCompare(b.date_obs));
    let current: ArchiveEntry[] = [];
    for (const entry of sorted) {
      const previous = current.at(-1);
      const gap = previous === undefined ? 0 : Date.parse(entry.date_obs) - Date.parse(previous.date_obs);
      if (previous !== undefined && gap > SESSION_GAP_MS) {
        sessions.push(sessionOf(target, current));
        current = [];
      }
      current.push(entry);
    }
    if (current.length > 0) sessions.push(sessionOf(target, current));
  }
  return sessions.sort((a, b) => b.startedAt.localeCompare(a.startedAt));
}

function sessionOf(target: string, entries: ArchiveEntry[]): ArchiveSession {
  const first = entries[0];
  const last = entries.at(-1);
  if (first === undefined || last === undefined) throw new Error("cannot build an empty archive session");
  return {
    id: `session:${target.toLocaleLowerCase()}:${first.date_obs}:${first.key}`,
    target,
    day: observedDay(first),
    startedAt: first.date_obs,
    endedAt: last.date_obs,
    entries,
  };
}

function parentOf(key: string): string | null {
  if (key === ".") return null;
  const slash = key.lastIndexOf("/");
  return slash < 0 ? "." : key.slice(0, slash);
}

export function buildArchiveFolders(directories: string[], entries: ArchiveEntry[]): ArchiveFolder[] {
  const keys = new Set<string>([".", ...directories]);
  for (const entry of entries) {
    let cursor = entry.directory;
    keys.add(cursor);
    while (cursor !== ".") {
      cursor = parentOf(cursor) ?? ".";
      keys.add(cursor);
    }
  }
  return [...keys]
    .sort((a, b) => a === "." ? -1 : b === "." ? 1 : a.localeCompare(b, undefined, { numeric: true }))
    .map((key) => {
      const prefix = key === "." ? "" : `${key}/`;
      const contained = key === "."
        ? entries
        : entries.filter((entry) => entry.directory === key || entry.directory.startsWith(prefix));
      return {
        key,
        name: key === "." ? "Capture folder" : key.split("/").at(-1) ?? key,
        parent: parentOf(key),
        depth: key === "." ? 0 : key.split("/").length,
        directFiles: entries.filter((entry) => entry.directory === key).length,
        totalFiles: contained.length,
        totalBytes: contained.reduce((sum, entry) => sum + entry.size_bytes, 0),
      };
    });
}

export function directEntries(entries: ArchiveEntry[], folder: string): ArchiveEntry[] {
  return entries.filter((entry) => entry.directory === folder);
}

export function childFolders(folders: ArchiveFolder[], parent: string): ArchiveFolder[] {
  return folders.filter((folder) => folder.parent === parent);
}

export function monthKey(isoDay: string): string {
  return isoDay.slice(0, 7);
}

export function shiftMonth(month: string, offset: number): string {
  const [yearText, monthText] = month.split("-");
  const date = new Date(Date.UTC(Number(yearText), Number(monthText) - 1 + offset, 1));
  return `${String(date.getUTCFullYear())}-${String(date.getUTCMonth() + 1).padStart(2, "0")}`;
}

export function calendarCells(
  month: string,
  entries: ArchiveEntry[],
  sessions: ArchiveSession[],
): CalendarCell[] {
  const [yearText, monthText] = month.split("-");
  const year = Number(yearText);
  const monthIndex = Number(monthText) - 1;
  const first = new Date(Date.UTC(year, monthIndex, 1));
  const mondayOffset = (first.getUTCDay() + 6) % 7;
  const start = new Date(Date.UTC(year, monthIndex, 1 - mondayOffset));
  const fileCounts = new Map<string, number>();
  const sessionCounts = new Map<string, number>();
  for (const entry of entries) fileCounts.set(observedDay(entry), (fileCounts.get(observedDay(entry)) ?? 0) + 1);
  for (const session of sessions) sessionCounts.set(session.day, (sessionCounts.get(session.day) ?? 0) + 1);
  return Array.from({ length: 42 }, (_, index) => {
    const date = new Date(start);
    date.setUTCDate(start.getUTCDate() + index);
    const day = date.toISOString().slice(0, 10);
    return {
      day,
      dayOfMonth: date.getUTCDate(),
      inMonth: date.getUTCMonth() === monthIndex,
      fileCount: fileCounts.get(day) ?? 0,
      sessionCount: sessionCounts.get(day) ?? 0,
    };
  });
}
