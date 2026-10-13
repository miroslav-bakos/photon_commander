#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Build offline VdB/LDN facts from pinned HEASARC TDAT inputs (no app network I/O)."""
from __future__ import annotations
import argparse
import csv
import hashlib
import io
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COLUMNS = ['designations','common_names','object_type','ra_j2000_deg','dec_j2000_deg','constellation','size_major_arcmin','size_minor_arcmin','position_angle_deg','magnitude','magnitude_band']

def read_tdat(path: Path, count: int):
    text = path.read_text()
    if not re.search(r'^equinox = 2000$', text, re.M):
        raise ValueError('Input must explicitly declare J2000')
    fields = next(line.split(' = ',1)[1].split() for line in text.splitlines() if line.startswith('line[1] = '))
    rows=[]
    for line in text.split('<DATA>\n',1)[1].split('<END>',1)[0].splitlines():
        if not line: continue
        values=line.split('|')
        if values[-1]=='': values.pop()
        if len(values)!=len(fields):raise ValueError('Invalid TDAT column count')
        row=dict(zip(fields,values,strict=True))
        ra,dec=float(row['ra']),float(row['dec'])
        if not 0<=ra<360 or not -90<=dec<=90:raise ValueError('Invalid J2000 position')
        rows.append(row)
    if len(rows)!=count:raise ValueError('Unexpected source count')
    return rows

def write_catalog(path, rows, source, input_path, table, changes):
    digest=hashlib.sha256(input_path.read_bytes()).hexdigest()
    header=f'''# SSC bundled offline nebula catalogue: {source}
# source: {source}, via NASA HEASARC
# license: Scientific catalogue facts; HEASARC open-data use policy
# license_url: https://heasarc.gsfc.nasa.gov/docs/heasarc/data_policy.html
# attribution: {source}; data provided by HEASARC, Astrophysics Science Division at NASA/GSFC.
# upstream: https://heasarc.gsfc.nasa.gov/W3Browse/nebula-catalog/{table}.html
# upstream_file: https://heasarc.gsfc.nasa.gov/FTP/heasarc/dbase/tdat_files/heasarc_{table}.tdat.gz
# upstream_tdat_sha256: {digest}
# fetched: 2026-10-13
# built_by: scripts/build-nebula-catalogs.py
# Changes: {changes}
# Only catalogue identifiers, J2000 positions and object classes are included.
# No descriptive prose, imagery, sizes inferred from area, or invented magnitudes.
# The source explicitly declares equinox=2000; positions are retained, not precessed.
'''
    output=io.StringIO();writer=csv.DictWriter(output,fieldnames=COLUMNS,lineterminator='\n');writer.writeheader()
    for row in rows:writer.writerow({k:row.get(k,'') for k in COLUMNS})
    path.write_text(header+output.getvalue())

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('ldn',type=Path);parser.add_argument('reflection',type=Path);args=parser.parse_args()
    ldn=[]
    for row in read_tdat(args.ldn,1791):
        if row['name']=='NN':continue
        if not re.fullmatch(r'LDN \d+',row['name']):raise ValueError('Unknown LDN identifier')
        ldn.append({'designations':row['name'],'object_type':'dark_nebula','ra_j2000_deg':row['ra'],'dec_j2000_deg':row['dec']})
    ldn.sort(key=lambda row:int(row['designations'].split()[1]))
    vdb=[]
    for row in read_tdat(args.reflection,913):
        name=row['vdb_name']
        if not name:continue
        common=''
        if name=='VDB 1a-d':common=name;name='VDB 1'
        if not re.fullmatch(r'VDB \d+[a-z]?',name):raise ValueError('Unknown VdB identifier')
        vdb.append({'designations':name.replace('VDB','VdB'),'common_names':common,'object_type':'diffuse_nebula' if row['nebula_type']=='C+E' else 'reflection_nebula','ra_j2000_deg':row['ra'],'dec_j2000_deg':row['dec']})
    vdb.sort(key=lambda row:(int(re.search(r'\d+',row['designations'])[0]),row['designations']))
    assert len(ldn)==1787 and len(vdb)==163
    folder=ROOT/'data/catalog'
    write_catalog(folder/'ldn.csv',ldn,'Lynds dark nebulae (1962, CDS revision1996)',args.ldn,'ldn','1787 named LDN rows;4 unnamed NN rows omitted; Barnard cross-identifications omitted to avoid merging distinct cloud sections.')
    write_catalog(folder/'vdb.csv',vdb,'VdB reflection nebulae, Magakian merged catalogue (2003)',args.reflection,'refnebulae','163 VDB-associated rows; VDB1a-d group exposed as VdB1 with its original group name retained; split components remain separate; NGC/IC and illuminating-star aliases omitted to avoid merging different centres; mixed C+E objects remain diffuse nebulae.')
    print(f'Wrote {len(ldn)} LDN and {len(vdb)} VdB entries')
if __name__=='__main__':main()
