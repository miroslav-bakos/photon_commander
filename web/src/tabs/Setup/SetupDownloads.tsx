// SPDX-License-Identifier: GPL-3.0-only
import { useState } from "react";
import { useLanguage } from "../../i18n/locale";

const upstream = "https://sourceforge.net/projects/astap-program/files/";
const platforms = {
  windows: { label: "Windows 64-bit (Intel / AMD)", program: "windows_installer/astap_setup.exe", database: "star_databases/d20_star_database.exe" },
  linux: { label: "Linux Debian / Ubuntu 64-bit (Intel / AMD)", program: "linux_installer/astap_amd64.deb", database: "star_databases/d20_star_database.deb" },
  pi: { label: "Raspberry Pi OS 64-bit", program: "linux_installer/astap_aarch64.deb", database: "star_databases/d20_star_database.deb" },
  mac: { label: "macOS Intel", program: "macOS%20installer/astap.pkg", database: "star_databases/d20_star_database.pkg" },
  apple: { label: "macOS Apple Silicon", program: "macOS%20installer/astap_M1.pkg", database: "star_databases/d20_star_database.pkg" },
};
const copy = {
  en: { title: "Install the plate solver", host: "Open these links on the computer running Photon Commander. If you are using a phone to control a PC or Raspberry Pi, install the files on that PC or Pi, not on the phone.", system: "Computer running Photon", choose: "Choose its operating system", program: "1. Download ASTAP", database: "2. Download D20 star database", install: "Download both files. On Windows, open each downloaded .exe file and follow the installer, keeping the default folders. On Linux or macOS, open the installation packages using your system's installer. ASTAP alone is not enough: install D20 too.", verify: "Then return to the ASTAP & star databases step and check the detected program and database. If they are missing, select their paths in that step and save.", other: "Other systems and official installation instructions", wide: "D20 is for the main camera. For a wide camera, also install W08 from the official download page. Apple Silicon users should follow the publisher's macOS instructions." },
  sk: { title: "Nainštalujte program na rozpoznanie hviezd", host: "Tieto odkazy otvorte na počítači, na ktorom beží Photon Commander. Ak telefónom ovládate PC alebo Raspberry Pi, súbory nainštalujte na dané PC alebo Pi, nie na telefón.", system: "Počítač, na ktorom beží Photon", choose: "Vyberte jeho operačný systém", program: "1. Stiahnuť ASTAP", database: "2. Stiahnuť hviezdnu databázu D20", install: "Stiahnite oba súbory. Vo Windows otvorte každý stiahnutý súbor .exe a prejdite inštalátorom; ponechajte predvolené priečinky. V Linuxe alebo macOS otvorte inštalačné balíky systémovým inštalátorom. Samotný ASTAP nestačí: nainštalujte aj D20.", verify: "Potom sa vráťte do kroku ASTAP a hviezdne databázy a skontrolujte nájdený program aj databázu. Ak chýbajú, v tomto kroku vyberte ich cesty a uložte ich.", other: "Ďalšie systémy a oficiálny postup inštalácie", wide: "D20 je pre hlavnú kameru. Pre širokouhlú kameru nainštalujte aj W08 z oficiálnej stránky. Na Apple Silicon postupujte podľa pokynov výrobcu pre macOS." },
  cs: { title: "Nainstalujte program pro rozpoznání hvězd", host: "Tyto odkazy otevřete na počítači, na kterém běží Photon Commander. Pokud telefonem ovládáte PC nebo Raspberry Pi, soubory nainstalujte na dané PC nebo Pi, ne na telefon.", system: "Počítač, na kterém běží Photon", choose: "Vyberte jeho operační systém", program: "1. Stáhnout ASTAP", database: "2. Stáhnout hvězdnou databázi D20", install: "Stáhněte oba soubory. Ve Windows otevřete každý stažený soubor .exe a projděte instalátorem; ponechte výchozí složky. V Linuxu nebo macOS otevřete instalační balíčky systémovým instalátorem. Samotný ASTAP nestačí: nainstalujte také D20.", verify: "Potom se vraťte do kroku ASTAP a hvězdné databáze a zkontrolujte nalezený program i databázi. Pokud chybí, v tomto kroku vyberte jejich cesty a uložte je.", other: "Další systémy a oficiální postup instalace", wide: "D20 je pro hlavní kameru. Pro širokoúhlou kameru nainstalujte také W08 z oficiální stránky. Na Apple Silicon postupujte podle pokynů výrobce pro macOS." },
};
export function SetupDownloads() {
  const text = copy[useLanguage()];
  const [system, setSystem] = useState<keyof typeof platforms | "">("");
  const selected = system ? platforms[system] : null;
  const linkClass = "inline-flex min-h-11 items-center rounded-md border border-border px-3 py-2 text-link underline";
  return <section className="space-y-3 rounded-lg border border-border p-4">
    <h3 className="font-semibold">{text.title}</h3>
    <p className="text-sm">{text.host}</p>
    <label className="block text-sm">{text.system}<select className="mt-1 min-h-11 w-full rounded-md border border-border bg-bg px-3 text-fg" value={system} onChange={(event) => setSystem(event.target.value as keyof typeof platforms | "")}>
      <option value="">{text.choose}</option>
      {Object.entries(platforms).map(([id, platform]) => <option key={id} value={id}>{platform.label}</option>)}
    </select></label>
    {selected && <div className="flex flex-wrap gap-2">
      <a className={linkClass} href={`${upstream}${selected.program}/download`} target="_blank" rel="noreferrer">{text.program}</a>
      <a className={linkClass} href={`${upstream}${selected.database}/download`} target="_blank" rel="noreferrer">{text.database}</a>
    </div>}
    <p className="text-sm">{text.install}</p>
    <p className="text-sm">{text.verify}</p>
    <p className="text-sm text-fg-muted">{text.wide}</p>
    <a className={linkClass} href="https://www.hnsky.org/astap.htm" target="_blank" rel="noreferrer">{text.other}</a>
  </section>;
}
