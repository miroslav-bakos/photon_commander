// SPDX-License-Identifier: GPL-3.0-only
import { useState } from "react";
import { useEphemerisSetupQuery } from "../../api/ephemeris";
import { Button } from "../../components/Button";
import { useLanguage } from "../../i18n/locale";

const copy = {
  en: {
    title: "Astronomy ephemeris · DE440s", loading: "Checking the server's ephemeris…", failed: "Unable to check the server's ephemeris.", retry: "Check again", ready: "Ready: the server has loaded its astronomy ephemeris. No download is needed.", missing: "The server has not loaded its astronomy ephemeris.", host: "Download the file on the computer running Photon Commander. If you control a PC or Raspberry Pi from a phone, move the file to that PC or Pi, not the phone.", download: "Download DE440s from NASA/JPL", move: "Create the destination folder if needed, then manually move the downloaded file there using exactly the filename shown below. Do not unzip or rename it.", restart: "After copying the file, restart Photon Commander on that computer when no observing or imaging is running. Then return here and check again. Copying alone does not load the file into the running server.", folder: "Destination folder on the server", file: "Complete file path", copy: "Copy folder address", copied: "Folder address copied.", copyFailed: "Unable to copy. Select the folder address and copy it manually.", integrity: "Expected file details", size: "Expected size (bytes)", digest: "SHA-256", error: "Server loading error",
  },
  sk: {
    title: "Astronomické efemeridy · DE440s", loading: "Kontrolujú sa efemeridy na serveri…", failed: "Efemeridy na serveri sa nepodarilo skontrolovať.", retry: "Skontrolovať znova", ready: "Pripravené: server načítal astronomické efemeridy. Nič netreba sťahovať.", missing: "Server nenačítal astronomické efemeridy.", host: "Súbor stiahnite na počítači, na ktorom beží Photon Commander. Ak telefónom ovládate PC alebo Raspberry Pi, presuňte súbor na dané PC alebo Pi, nie do telefónu.", download: "Stiahnuť DE440s z NASA/JPL", move: "Ak cieľový priečinok neexistuje, vytvorte ho. Stiahnutý súbor doň ručne presuňte s presným názvom uvedeným nižšie. Súbor nerozbaľujte ani nepremenúvajte.", restart: "Po skopírovaní reštartujte Photon Commander na danom počítači, keď neprebieha pozorovanie ani snímanie. Potom sa sem vráťte a skontrolujte stav znova. Samotné skopírovanie súbor nenačíta do bežiaceho servera.", folder: "Cieľový priečinok na serveri", file: "Úplná cesta k súboru", copy: "Kopírovať adresu priečinka", copied: "Adresa priečinka skopírovaná.", copyFailed: "Kopírovanie sa nepodarilo. Označte adresu priečinka a skopírujte ju ručne.", integrity: "Očakávané údaje súboru", size: "Očakávaná veľkosť (bajty)", digest: "SHA-256", error: "Chyba načítania na serveri",
  },
  cs: {
    title: "Astronomické efemeridy · DE440s", loading: "Kontrolují se efemeridy na serveru…", failed: "Efemeridy na serveru se nepodařilo zkontrolovat.", retry: "Zkontrolovat znovu", ready: "Připraveno: server načetl astronomické efemeridy. Nic není třeba stahovat.", missing: "Server nenačetl astronomické efemeridy.", host: "Soubor stáhněte na počítači, na kterém běží Photon Commander. Pokud telefonem ovládáte PC nebo Raspberry Pi, přesuňte soubor na dané PC nebo Pi, ne do telefonu.", download: "Stáhnout DE440s z NASA/JPL", move: "Pokud cílová složka neexistuje, vytvořte ji. Stažený soubor do ní ručně přesuňte s přesným názvem uvedeným níže. Soubor nerozbalujte ani nepřejmenovávejte.", restart: "Po zkopírování restartujte Photon Commander na daném počítači, když neprobíhá pozorování ani snímání. Potom se sem vraťte a zkontrolujte stav znovu. Samotné zkopírování soubor nenačte do běžícího serveru.", folder: "Cílová složka na serveru", file: "Úplná cesta k souboru", copy: "Kopírovat adresu složky", copied: "Adresa složky zkopírována.", copyFailed: "Kopírování se nepodařilo. Označte adresu složky a zkopírujte ji ručně.", integrity: "Očekávané údaje souboru", size: "Očekávaná velikost (bajty)", digest: "SHA-256", error: "Chyba načítání na serveru",
  },
};

export function SetupEphemeris() {
  const text = copy[useLanguage()];
  const query = useEphemerisSetupQuery();
  const [copied, setCopied] = useState<"success" | "failed" | null>(null);
  async function copyFolder() {
    if (!query.data) return;
    try {
      await navigator.clipboard.writeText(query.data.folder_path);
      setCopied("success");
    } catch { setCopied("failed"); }
  }
  return <section aria-labelledby="setup-ephemeris-title" className="space-y-3 rounded-lg border border-border p-4">
    <h3 id="setup-ephemeris-title" className="font-semibold">{text.title}</h3>
    {query.isPending ? <p role="status">{text.loading}</p> : query.isError || !query.data ? <>
      <p role="alert">{text.failed}</p>
      <Button onClick={() => { void query.refetch(); }}>{text.retry}</Button>
    </> : <>
      <p role="status" className={query.data.loaded ? "text-success" : "text-warning"}>{query.data.loaded ? text.ready : text.missing}</p>
      {!query.data.loaded && <>
        <p className="text-sm">{text.host}</p>
        <a className="inline-flex min-h-11 items-center text-link underline" href={query.data.download_url} target="_blank" rel="noreferrer">{text.download}</a>
        <p className="text-sm">{text.move}</p>
      </>}
      <label className="block text-sm">{text.folder}<input className="mt-1 min-h-11 w-full rounded-md border border-border bg-bg px-3 text-fg" readOnly value={query.data.folder_path} onFocus={(event) => { event.currentTarget.select(); }} /></label>
      <Button onClick={() => { void copyFolder(); }}>{text.copy}</Button>
      {copied && <p role="status" className="text-sm">{copied === "success" ? text.copied : text.copyFailed}</p>}
      <p className="text-sm">{text.file}: <code className="break-all">{query.data.kernel_path}</code></p>
      {!query.data.loaded && <>
        {query.data.error && <p className="break-words whitespace-pre-wrap text-sm text-warning">{text.error}: {query.data.error}</p>}
        <details className="text-sm"><summary className="min-h-11 cursor-pointer">{text.integrity}</summary>
          <p>{query.data.filename}</p><p>{text.size}: {query.data.expected_size_bytes.toLocaleString()}</p><p className="break-all">{text.digest}: {query.data.expected_sha256}</p>
        </details>
        <p className="text-sm">{text.restart}</p>
      </>}
      <Button disabled={query.isFetching} onClick={() => { void query.refetch(); }}>{text.retry}</Button>
    </>}
  </section>;
}
