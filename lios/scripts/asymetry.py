import matplotlib.pyplot as plt
import numpy as np

# Your data
forwarded = {
    "amazon_project_kuiper": 112139298.47605765,
    "ast_spacemobile": 3981.9235016794937,
    "china_satnet_guowang": 121006.10990686345,
    "echostar": 82713.69722152415,
    "eutelsat_oneweb": 0.0,
    "g60_starlink": 12995767.748383222,
    "globalstar": 0.0,
    "inmarsat": 282229.60721985484,
    "intelsat": 1981919.5457271005,
    "iridium_communications": 4251624.839958848,
    "kepler_communications": 0.0,
    "lynk_global": 100012.00374170872,
    "orbcomm": 70037.12281367872,
    "planet_labs": 20688418.7651734,
    "ses": 1377769.1213727538,
    "spacex_starlink": 4813337596.041415,
    "spire_global": 14123796.233581467,
    "viasat": 44829.43340313237,
}
received = {
    "amazon_project_kuiper": 2045642062.5101874,
    "ast_spacemobile": 9067262.523790546,
    "china_satnet_guowang": 65216077.933279194,
    "echostar": 212097.52719826775,
    "eutelsat_oneweb": 0.0,
    "g60_starlink": 134304307.5559509,
    "globalstar": 0.0,
    "inmarsat": 962422.3799272157,
    "intelsat": 1451493.0342887957,
    "iridium_communications": 10293082.448663933,
    "kepler_communications": 0.0,
    "lynk_global": 56718943.265425265,
    "orbcomm": 6573549.033126128,
    "planet_labs": 1207335136.5534606,
    "ses": 765413.9296773847,
    "spacex_starlink": 144624555.99022704,
    "spire_global": 1298056561.4504728,
    "viasat": 378034.5338527027,
}

# Sort companies by TOTAL traffic for a tidy diagram
companies = sorted(forwarded.keys(), key=lambda x: forwarded[x] + received[x], reverse=True)
f_vals = [forwarded[c] for c in companies]
r_vals = [received[c] for c in companies]
y_pos = np.arange(len(companies))

# Plotting
fig, ax = plt.subplots(figsize=(14, 10))
bar_height = 0.35

# Replace 0 with a very small number (1e-6) to avoid log(0) issues, but keep visual clarity.
# Matplotlib handles log scale with 0 by ignoring them, but we'll keep 0 as is and use log.
# We'll set a minimum positive value for the log scale to not break.
ax.barh(y_pos - bar_height/2, f_vals, bar_height, label='Forwarded (Outgoing)', color='#1f77b4', edgecolor='black', linewidth=0.5)
ax.barh(y_pos + bar_height/2, r_vals, bar_height, label='Received (Incoming)', color='#ff7f0e', edgecolor='black', linewidth=0.5)

# Log scale x-axis (essential for this data)
ax.set_xscale('log')
# Filter out zeros for setting x-limits, but matplotlib handles it gracefully.
ax.set_xlim(1e-1, 1e10)  # Wide enough to cover all values

ax.set_yticks(y_pos)
ax.set_yticklabels(companies, fontsize=10)
ax.set_xlabel('Bytes Transferred (Log Scale)', fontsize=12)
ax.set_title('Extreme Asymmetry: Bytes Forwarded vs. Received per Satellite Network', fontsize=14, fontweight='bold')
ax.legend(fontsize=12)
ax.grid(axis='x', linestyle='--', alpha=0.5)

# Add value annotations to show the imbalance directly (optional, uncomment to use)
# for i, (f, r) in enumerate(zip(f_vals, r_vals)):
#     ax.text(f*1.5, i - 0.2, f'{f:.1e}', va='center', ha='right', fontsize=8)
#     ax.text(r*1.5, i + 0.2, f'{r:.1e}', va='center', ha='right', fontsize=8)

plt.tight_layout()
plt.show()