// SPDX-License-Identifier: GPL-3.0-only
import { useEffect, useRef, useState } from "react";
import { useLocation } from "react-router-dom";
import { readClientConfiguration } from "../api/settings";
import { Button } from "../components/Button";
import { Card } from "../components/Card";
import { SETUP_STORAGE_KEY, SetupLink } from "../tabs/Setup/SetupTab";
import { useLanguage } from "../i18n/locale";

/** Offer help without redirecting an existing observing session. Unknown server
 * preferences leave the offer hidden; restored preferences remain authoritative. */
export function SetupOffer() {
  const [visible, setVisible] = useState(false);
  const { pathname } = useLocation();
  const language = useLanguage();
  const enteredSetup = useRef(pathname === "/setup");
  useEffect(() => {
    let active = true;
    void readClientConfiguration().then((configuration) => {
      if (active && !enteredSetup.current) setVisible(configuration.entries[SETUP_STORAGE_KEY] === undefined);
    }).catch(() => undefined);
    return () => { active = false; };
  }, []);
  useEffect(() => {
    if (pathname === "/setup") {
      enteredSetup.current = true;
    }
  }, [pathname]);
  if (pathname === "/setup" && visible) setVisible(false);
  if (!visible || pathname === "/setup") return null;
  const labels = {
    en: ["First-time setup", "Continue without the guide"],
    sk: ["Prvotné nastavenie", "Pokračovať bez sprievodcu"],
    cs: ["Prvotní nastavení", "Pokračovat bez průvodce"],
  }[language];
  return <Card className="mb-4"><div className="flex flex-wrap items-center gap-3">
    <strong>{labels[0]}</strong><SetupLink />
    <Button onClick={() => { setVisible(false); }}>{labels[1]}</Button>
  </div></Card>;
}
