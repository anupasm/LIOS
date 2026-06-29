#!/bin/bash -l
#SBATCH --job-name=lios_experiments
#SBATCH --partition=cs
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --time=2-00:00:00
#SBATCH --output=logs/experiments_%j.out
#SBATCH --error=logs/experiments_%j.err
#SBATCH --mail-type=BEGIN,END,FAIL
#SBATCH --mail-user=anupa.desilva@ucd.ie

# Usage:
#   sbatch run_experiments.slurm
#   sbatch run_experiments.slurm lios_constellation_weighted
#   sbatch run_experiments.slurm ground_reset_constellation_weighted

set -euo pipefail

REPO_DIR="${SLURM_SUBMIT_DIR:-$HOME/LIOS}"
CACHE_DIR="/scratch/$USER/lios_cache"
RESULTS_DIR="/scratch/$USER/lios_results/${SLURM_JOB_ID}"
CONFIG="${1:-all}"

CONTACT_PLAN="cp_24h_step30_range4000.csv"
CONTACT_META="cp_24h_step30_range4000_meta.json"
TRAFFIC_SCHEDULE="tf_pair_v2_e20260614T000000_d0.500_w863a161b79_24h_s42_r0.300000_step30_range4000.json"

case "$CONFIG" in
    all|lios_constellation_weighted|ground_reset_constellation_weighted)
        ;;
    *)
        echo "Unknown experiment config: $CONFIG" >&2
        echo "Expected: all, lios_constellation_weighted, or ground_reset_constellation_weighted" >&2
        exit 2
        ;;
esac

for artifact in "$CONTACT_PLAN" "$CONTACT_META" "$TRAFFIC_SCHEDULE"; do
    if [[ ! -s "$CACHE_DIR/$artifact" ]]; then
        echo "Required precompute artifact is missing or empty: $CACHE_DIR/$artifact" >&2
        echo "Run: sbatch run_precompute.slurm" >&2
        exit 1
    fi
done

mkdir -p "$REPO_DIR/logs"
mkdir -p "$RESULTS_DIR"
mkdir -p "$RESULTS_DIR/matplotlib"

module load python/3.11.9-gcc-11.5.0-lcos74i
source "$REPO_DIR/.env/bin/activate"

export MPLCONFIGDIR="$RESULTS_DIR/matplotlib"
export OMP_NUM_THREADS="$SLURM_CPUS_PER_TASK"
export MKL_NUM_THREADS="$SLURM_CPUS_PER_TASK"

cd "$REPO_DIR"

echo "Job: $SLURM_JOB_ID"
echo "Host: $(hostname)"
echo "Started: $(date)"
echo "Experiment: $CONFIG"
echo "Cache: $CACHE_DIR"
echo "Results: $RESULTS_DIR"
echo "CPUs: $SLURM_CPUS_PER_TASK"
echo ""

command=(
    python -u lios/evaluation/run_experiments.py
    --data "$REPO_DIR/lios/data"
    --cache "$CACHE_DIR"
    --out "$RESULTS_DIR"
)

if [[ "$CONFIG" != "all" ]]; then
    command+=(--config "$CONFIG")
fi

"${command[@]}"

echo ""
echo "Experiments finished: $(date)"
echo "Results size:"
du -sh "$RESULTS_DIR"
echo "Results files:"
find "$RESULTS_DIR" -maxdepth 2 -type f -printf '%p\n' | sort
