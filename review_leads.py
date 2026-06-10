#!/usr/bin/env python3
"""
review_leads.py — Script CLI autônomo para recuperação de leads perdidos da Meta Ads.

Pode ser executado manualmente ou agendado via cron (recomendado em produção).

Uso:
    python review_leads.py               # Janela padrão: 6 horas
    python review_leads.py --hours 24    # Janela customizada: 24 horas

Exemplo de entrada no crontab (a cada 6 horas):
    0 */6 * * * /caminho/para/.venv/bin/python /caminho/para/review_leads.py --hours 6 >> /var/log/meta_review.log 2>&1

Para Docker / VPS com docker-compose:
    docker exec meta-leads-hub-instance python review_leads.py --hours 6
"""
import argparse
import logging
import sys
from pathlib import Path

# Garante que o diretório raiz do projeto está no PYTHONPATH
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("review_leads")


def main():
    parser = argparse.ArgumentParser(
        description="Revisar e recuperar leads perdidos da Meta Ads.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=6,
        help="Janela de tempo em horas para buscar leads (padrão: 6).",
    )
    args = parser.parse_args()

    if args.hours < 1 or args.hours > 168:
        logger.error("--hours deve ser entre 1 e 168 (1 semana).")
        sys.exit(1)

    logger.info(f"Iniciando revisão de leads (janela: {args.hours} horas)...")

    try:
        from database import get_database
        import services

        db = get_database()
        results = services.review_and_recover_leads(db, hours=args.hours, trigger="cli")

    except Exception as e:
        logger.error(f"Erro fatal durante a revisão: {e}", exc_info=True)
        sys.exit(1)

    # ── Relatório final ──
    print("\n" + "=" * 55)
    print("  Meta Leads Hub — Relatório de Revisão")
    print("=" * 55)
    print(f"  Janela de busca     : últimas {args.hours} horas")
    print(f"  Leads encontrados   : {results['leads_found_in_meta']} (na Meta Graph API)")
    print(f"  Leads recuperados   : {results['recovered_leads']} (novos no DB local)")
    print(f"  Duplicatas ignoradas: {results['skipped_duplicates']} (já existiam)")
    print(f"  Erros               : {results['errors']}")
    if "duration_seconds" in results:
        print(f"  Duração             : {results['duration_seconds']:.1f}s")
    print("=" * 55 + "\n")

    if results["errors"] > 0:
        logger.warning(f"{results['errors']} erro(s) ocorreram durante a revisão. Verifique os logs acima.")
        sys.exit(2)  # Exit code 2: warnings/partial errors

    sys.exit(0)


if __name__ == "__main__":
    main()
