// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/** Planning's compact mount-mode, mosaic, and map-only FOV controls. */
import { useEffect, useRef, useState, type ReactNode } from "react";
import { Grid2X2, X } from "lucide-react";
import { SegmentedControl } from "../../components/SegmentedControl";
import type { MountMode, ProfileSummary } from "../../api/astro";
import {
  MAP_TOOLBAR_BUTTON_SIZE_CLASSES,
  MAP_TOOLBAR_GLYPH_CLASSES,
  MAP_TOOLBAR_PANEL_CLASSES,
} from "../../skymap/mapToolbarStyle";

const ICON_BUTTON_CLASSES = `flex items-center justify-center rounded-md text-fg hover:bg-accent-muted hover:text-accent focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent ${MAP_TOOLBAR_BUTTON_SIZE_CLASSES}`;

export function SessionControls({
  profiles,
  profileId,
  mount,
  opticId = null,
  onOpticChange,
  mosaicPanel,
  onProfileChange,
  onMountChange,
}: {
  profiles: ProfileSummary[];
  profileId: string | null;
  mount: MountMode;
  opticId?: string | null;
  onOpticChange?: (opticId: string) => void;
  mosaicPanel: ReactNode;
  onProfileChange: (profileId: string) => void;
  onMountChange: (mount: MountMode) => void;
}) {
  const [profilesOpen, setProfilesOpen] = useState(false);
  const [mosaicOpen, setMosaicOpen] = useState(false);

  const mosaicTrigger = useRef<HTMLButtonElement>(null);
  const profileTrigger = useRef<HTMLButtonElement>(null);
  const mosaicDialog = useRef<HTMLDivElement>(null);
  const mosaicClose = useRef<HTMLButtonElement>(null);
  const profileList = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!mosaicOpen) return;
    mosaicClose.current?.focus();
    const dialog = mosaicDialog.current;
    const dismiss = (event: KeyboardEvent) => {
      if (event.key === "Escape" && dialog?.contains(event.target as Node)) {
        event.preventDefault();
        event.stopPropagation();
        setMosaicOpen(false);
        mosaicTrigger.current?.focus();
      }
    };
    dialog?.addEventListener("keydown", dismiss);
    return () => { dialog?.removeEventListener("keydown", dismiss); };
  }, [mosaicOpen]);
  useEffect(() => {
    if (profilesOpen) {
      const list = profileList.current;
      (list?.querySelector<HTMLButtonElement>('[aria-selected="true"]')
        ?? list?.querySelector<HTMLButtonElement>('[role="option"]'))?.focus();
    }
  }, [profilesOpen]);

  const closeMosaic = () => {
    setMosaicOpen(false);
    mosaicTrigger.current?.focus();
  };
  const closeProfiles = () => {
    setProfilesOpen(false);
    profileTrigger.current?.focus();
  };

  return (
    <div className={`relative max-w-full flex-wrap ${MAP_TOOLBAR_PANEL_CLASSES}`} role="toolbar" aria-label="Planning controls">
      <SegmentedControl label="Mount mode" value={mount} onChange={onMountChange}
        options={[{ value: "altaz", label: "ALT/AZ", icon: <MountGlyph equatorial={false} />, title: "ALT/AZ mount preview" }, { value: "eq", label: "EQ", icon: <MountGlyph equatorial />, title: "Equatorial mount preview" }]} />
      <button
        type="button"
        className={ICON_BUTTON_CLASSES}
        ref={mosaicTrigger}
        aria-haspopup="dialog"
        aria-label="Configure mosaic"
        aria-expanded={mosaicOpen}
        title="Configure mosaic"
        onClick={() => {
          setProfilesOpen(false);
          setMosaicOpen((open) => !open);
        }}
      >
        <Grid2X2 aria-hidden className={MAP_TOOLBAR_GLYPH_CLASSES} />
      </button>
      <button
        type="button"
        className={ICON_BUTTON_CLASSES}
        ref={profileTrigger}
        aria-haspopup="listbox"
        aria-label="Choose skymap FOV profile"
        aria-expanded={profilesOpen}
        title="Choose skymap FOV profile"
        onClick={() => {
          setMosaicOpen(false);
          setProfilesOpen((open) => !open);
        }}
      >
        <FovProfileGlyph />
      </button>
      {onOpticChange && (
        <SegmentedControl label="Planning lens" value={opticId ?? ""} onChange={onOpticChange}
          options={(profiles.find((profile) => profile.id === profileId)?.optics ?? []).map((optic) => ({
            value: optic.id, label: optic.id === "tele" ? "Tele" : optic.id === "wide" ? "Wide" : optic.label,
            icon: optic.id === "wide" || optic.id === "tele" ? <LensGlyph wide={optic.id === "wide"} /> : undefined,
            title: `${optic.label} field of view preview`,
          }))} />
      )}
      {profilesOpen && (
        <div
          tabIndex={-1}
          ref={profileList}
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              event.preventDefault();
              event.stopPropagation();
              closeProfiles();
              return;
            }
            const options = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="option"]'));
            const index = options.indexOf(document.activeElement as HTMLButtonElement);
            let next: number;
            if (event.key === "ArrowDown") next = (index + 1) % options.length;
            else if (event.key === "ArrowUp") next = (index - 1 + options.length) % options.length;
            else if (event.key === "Home") next = 0;
            else if (event.key === "End") next = options.length - 1;
            else return;
            event.preventDefault();
            options[next]?.focus();
          }}
          role="listbox"
          aria-label="Skymap FOV profiles"
          className="absolute bottom-[calc(100%+0.5rem)] right-0 min-w-56 overflow-hidden rounded-lg border border-border-strong bg-surface-raised p-1 text-sm shadow-xl"
        >
          {profiles.map((profile) => (
            <button
              key={profile.id}
              type="button"
              role="option"
              aria-selected={profile.id === profileId}
              className={`flex min-h-11 w-full items-center justify-between gap-3 rounded-md px-3 py-2 text-left ${profile.id === profileId ? "bg-accent-muted text-accent" : "text-fg hover:bg-surface"}`}
              onClick={() => {
                onProfileChange(profile.id);
                closeProfiles();
              }}
            >
              <span>{profile.name}</span>
              {profile.id === profileId && <span aria-hidden>✓</span>}
            </button>
          ))}
        </div>
      )}
      {mosaicOpen && (
        <div
          ref={mosaicDialog}
          role="dialog"
          aria-label="Mosaic configuration"
          className="absolute bottom-[calc(100%+0.5rem)] right-0 max-h-[min(70vh,32rem)] w-[min(22rem,calc(100vw-2rem))] overflow-y-auto rounded-lg border border-border-strong bg-surface-raised p-3 text-sm shadow-xl"
        >
          <div className="mb-3 flex items-center justify-between gap-3">
            <h3 className="text-sm font-semibold tracking-wide text-fg-muted uppercase">Mosaic</h3>
            <button
              type="button"
              className={ICON_BUTTON_CLASSES}
              ref={mosaicClose}
              aria-label="Close mosaic configuration"
              title="Close mosaic configuration"
              onClick={closeMosaic}
            >
              <X aria-hidden className={MAP_TOOLBAR_GLYPH_CLASSES} />
            </button>
          </div>
          {mosaicPanel}
        </div>
      )}
    </div>
  );
}

function Glyph({ children }: { children: ReactNode }) {
  return <svg aria-hidden viewBox="0 0 24 24" className={MAP_TOOLBAR_GLYPH_CLASSES} fill="none" stroke="currentColor" strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round">{children}</svg>;
}

function FovProfileGlyph() {
  return <Glyph><rect x="4" y="6" width="16" height="12" rx="1.5" /><path d="M8 3v3m8-3v3M8 18v3m8-3v3" /></Glyph>;
}

/** The tripod stays upright; equatorial mode tilts the instrument on its wedge. */
function MountGlyph({ equatorial }: { equatorial: boolean }) {
  return <Glyph>
    <path d="M12 15v2m0 0-6 5m6-5 6 5m-6-5v5" />
    <g transform={equatorial ? "rotate(-30 12 15)" : undefined}>
      <rect x="8" y="2" width="8" height="12" rx="2" />
      <circle cx="12" cy="5.5" r="1.5" />
      <path d="M9 15h6" />
    </g>
  </Glyph>;
}

/** Wide and tele show the same optical axis with different fields of view. */
function LensGlyph({ wide }: { wide: boolean }) {
  return <Glyph>
    <circle cx="5" cy="12" r="2" />
    <path d={wide ? "M7 11 21 3v18L7 13" : "M7 11 21 8v8L7 13"} />
    <path d="M11 12h7" />
  </Glyph>;
}
