"""Comparativo de qualidade entre gravações.

    python manage.py quality_report dados/ --codes --until-marker 5 --csv qualidade.csv

Percorre os CSVs informados (arquivos ou pastas, recursivo), roda as verificações
de signals/quality.py e imprime/salva uma tabela com uma linha por gravação.
Com --codes, as gravações viram P01, P02, ... (ordem alfabética dos caminhos) e o
mapeamento código -> arquivo é salvo à parte, para não expor nomes no relatório.
"""
import csv
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from signals.quality import compare_recordings


class Command(BaseCommand):
    help = "Roda as verificações de qualidade em várias gravações e gera um comparativo."

    def add_arguments(self, parser):
        parser.add_argument("paths", nargs="+", help="CSVs ou pastas com CSVs")
        parser.add_argument("--pattern", default="*.csv", help="padrão de arquivo nas pastas (padrão: *.csv)")
        parser.add_argument("--until-marker", type=int, default=None,
                            help="corta cada gravação no k-ésimo marcador (ex.: 5)")
        parser.add_argument("--codes", action="store_true", help="rotula as gravações como P01, P02, ...")
        parser.add_argument("--csv", default=None, help="salva o comparativo neste arquivo CSV")

    def handle(self, *args, **opt):
        files = []
        for p in map(Path, opt["paths"]):
            files += sorted(p.rglob(opt["pattern"])) if p.is_dir() else [p]
        if not files:
            raise CommandError("Nenhum CSV encontrado.")
        files = sorted(files, key=str)
        labels = [f"P{i + 1:02d}" for i in range(len(files))] if opt["codes"] else [str(f) for f in files]
        rows = compare_recordings(files, labels, opt["until_marker"])

        cols = ["gravacao", "ok", "rede_mV", "saturacao_pct", "canais_mortos", "canais_identicos",
                "saltos", "linhas_duplicadas", "borda_razao", "n_problemas"]
        widths = {c: max(len(c), *(len(str(r[c])) for r in rows)) for c in cols}
        self.stdout.write("  ".join(c.ljust(widths[c]) for c in cols))
        for r in rows:
            self.stdout.write("  ".join(str(r[c]).ljust(widths[c]) for c in cols))
        ok = sum(r["ok"] for r in rows)
        self.stdout.write(f"\n{ok} de {len(rows)} gravações aprovadas em todas as verificações.")

        if opt["csv"]:
            out = Path(opt["csv"])
            with out.open("w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=list(rows[0]))
                w.writeheader(); w.writerows(rows)
            self.stdout.write(self.style.SUCCESS(f"Comparativo salvo em {out}"))
            if opt["codes"]:
                mp = out.with_name(out.stem + "_mapeamento.csv")
                with mp.open("w", newline="", encoding="utf-8") as fh:
                    w = csv.writer(fh); w.writerow(["codigo", "arquivo"])
                    w.writerows(zip(labels, map(str, files)))
                self.stdout.write(f"Mapeamento código -> arquivo em {mp} (não publicar)")
