#!/bin/bash
#SBATCH --job-name=lios_precompute
#SBATCH --partition=high2          # high-memory partition; use bigmem2 if available/needed
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=32
#SBATCH --mem=256G                 # see memory note below
#SBATCH --time=12:00:00
#SBATCH --output=logs/precompute_%j.out
#SBATCH --error=logs/precompute_%j.err
#SBATCH --mail-type=BEGIN,END,FAIL
#SBATCH --mail-user=shyamdeanuwa@gmail.com

# ── Memory budget (adjust --mem if you hit OOM) ─────────────────────────────
#
#  Phase 2 (route table build): main process holds the full route_table dict
#  in RAM while submitting to workers. Rough estimate: 2-5 GB for a 3-operator,
#  ~150-sat constellation across 288 five-minute buckets.
#
#  Phase 3 (traffic): each worker independently json.load()s the full route
#  table from the .json.gz file. With 32 workers and a 3 GB uncompressed table,
#  peak RAM ≈ 32 × 3 GB = ~96 GB, plus the main process accumulating all_records.
#  256 GB is conservative-safe; drop to 128 GB if the cluster is congested and
#  you've confirmed your route table is small.
#
#  To check actual usage after a run:
#    sacct -j $SLURM_JOB_ID --format=JobID,MaxRSS,Elapsed
# ────────────────────────────────────────────────────────────────────────────

set -euo pipefail

REPO_DIR="$HOME/LIOS"        # adjust if your clone is elsewhere
CACHE_DIR="/scratch/$USER/lios_cache"

mkdir -p "$REPO_DIR/logs"
mkdir -p "$CACHE_DIR"

echo "Job $SLURM_JOB_ID started on $(hostname) at $(date)"
echo "CPUs allocated: $SLURM_CPUS_PER_TASK"
echo "Cache dir: $CACHE_DIR"
echo ""

# ── Load required modules (adjust to what Sonic has) ────────────────────────
# Check available: module spider python
module load python/3.11         # or python/3.10 — whatever Sonic provides
# module load anaconda3         # alternative if using conda

# If using a virtualenv or conda env, activate it here:
source "$HOME/LIOS/.env/source/bin/activate"
# conda activate lios

cd "$REPO_DIR"

# ── Run the precompute ───────────────────────────────────────────────────────
python precompute_hpc.py \
    --workers "$SLURM_CPUS_PER_TASK" \
    --cache   "$CACHE_DIR" \
    --step    30 \
    --range   2000 \
    --seeds   42 \
    --rates   0.01

echo ""
echo "Precompute finished at $(date)"
echo "Cache contents:"
du -sh "$CACHE_DIR"/*  2>/dev/null || echo "(empty)"
