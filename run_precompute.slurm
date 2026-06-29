#!/bin/bash -l
#SBATCH --job-name=lios_precompute
#SBATCH --partition=cs
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=32
#SBATCH --time=12:00:00
#SBATCH --output=logs/precompute_%j.out
#SBATCH --error=logs/precompute_%j.err
#SBATCH --mail-type=BEGIN,END,FAIL
#SBATCH --mail-user=anupa.desilva@ucd.ie

# Sonic rejected an explicit --mem request for this partition. The job uses the
# partition default; inspect MaxRSS with sacct after completion.

set -euo pipefail

REPO_DIR="${SLURM_SUBMIT_DIR:-$HOME/LIOS}"
CACHE_DIR="/scratch/$USER/lios_cache"

mkdir -p "$REPO_DIR/logs"
mkdir -p "$CACHE_DIR"

echo "Job $SLURM_JOB_ID started on $(hostname) at $(date)"
echo "CPUs allocated: $SLURM_CPUS_PER_TASK"
echo "Cache dir: $CACHE_DIR"
echo ""

# ── Load required modules (adjust to what Sonic has) ────────────────────────
# Check available: module avail python   OR   module avail anaconda
module load python/3.11.9-gcc-11.5.0-lcos74i

# If using a virtualenv or conda env, activate it here:
source "$REPO_DIR/.env/bin/activate"
# conda activate lios

cd "$REPO_DIR"

# ── Run the precompute ───────────────────────────────────────────────────────
python precompute_hpc.py \
    --workers "$SLURM_CPUS_PER_TASK" \
    --cache   "$CACHE_DIR" \
    --step    30 \
    --seeds   42 \
    --rates   0.3

echo ""
echo "Precompute finished at $(date)"
echo "Cache contents:"
du -sh "$CACHE_DIR"/*  2>/dev/null || echo "(empty)"
