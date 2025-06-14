import numpy as np
import skrf as rf
from joblib import Parallel, delayed
from matplotlib import pyplot as plt
from matplotlib.colors import ListedColormap
from scipy.signal import butter, filtfilt
from sklearn.metrics import accuracy_score
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVC
from tqdm import tqdm
from pulse import Pulse, TransitedPulse, ReflectedPulse, ReadoutPulse
from utils import UnitConverter

"""
Fidelity simulation based on the paper

Wong, Hiu Yung, Prabjot Dhillon, Kristin M. Beck, and Yaniv J. Rosen. 2023. 
“A Simulation Methodology for Superconducting Qubit Readout Fidelity.” 
Solid-State Electronics 201 (March):108582. https://doi.org/10.1016/j.sse.2022.108582.
"""


class FidelitySimulation:
    def __init__(
            self,
            readout_pulse: ReadoutPulse,
            s_parameters_file_state_0: str,
            s_parameters_file_state_1: str,
            IQ_projection_frequency: int,
            # readout_type can be "transition" or "reflection"
            readout_type: str = "transition",
            num_iterations: int = 50,
            noise_parameters: dict = None,
            # only needed if IQ_projection_frequency not the same as carrier_frequency of the readout pulse
            readout_dt: float = None,
    ):

        self.s_parameters_file_state_0 = s_parameters_file_state_0
        self.s_parameters_file_state_1 = s_parameters_file_state_1
        self.readout_pulse = readout_pulse
        self.readout_type = readout_type
        self.IQ_projection_frequency = IQ_projection_frequency
        self.num_iterations = num_iterations
        self.readout_dt = readout_dt

        # Noise parameters based on the paper
        if noise_parameters is None:
            noise_parameters = {
                "quantum_noise": {
                    "type": "quantum",
                    "T_ns": 0.5,  # K, from paper
                },
                "thermal_noise_room_temp": {
                    "type": "thermal",
                    "T_eff": 1.5,  # K, from paper
                    "bandwidth": 6e9,  # Hz (6 GHz), from paper
                    "resistance": 50.0  # Ohms, common impedance
                },
                "thermal_noise_hemt": {
                    "type": "thermal",
                    "T_eff": 54,  # K, from paper
                    "bandwidth": 6e9,  # Hz (6 GHz), from paper
                    "resistance": 50.0  # Ohms
                }
            }
        self.noise_parameters = noise_parameters

    def run(self) -> float:
        ntw_state_0 = rf.Network(self.s_parameters_file_state_0)
        ntw_state_1 = rf.Network(self.s_parameters_file_state_1)

        if self.readout_type == "transition":
            signal_state_0 = TransitedPulse(original_pulse=self.readout_pulse, ntw=ntw_state_0,
                                            name="Transited Pulse |0>")
            signal_state_1 = TransitedPulse(original_pulse=self.readout_pulse, ntw=ntw_state_1,
                                            name="Transited Pulse |1>")
        elif self.readout_type == "reflection":
            signal_state_0 = ReflectedPulse(original_pulse=self.readout_pulse, ntw=ntw_state_0,
                                            name="Reflected Pulse |0>")
            signal_state_1 = ReflectedPulse(original_pulse=self.readout_pulse, ntw=ntw_state_1,
                                            name="Reflected Pulse |1>")
        else:
            raise NotImplementedError

        I_state_0, Q_state_0 = self._IQ_projection_demodulation(signal_from_system=signal_state_0)
        I_state_1, Q_state_1 = self._IQ_projection_demodulation(signal_from_system=signal_state_1)

        fidelity = self._calculate_fidelity(I_state_0, Q_state_0, I_state_1, Q_state_1, plot_results=True)

        return fidelity

    # Noise independent of signal power
    def _create_noise(self, signal_from_system: Pulse) -> np.ndarray:
        k = 1.3806e-23  # Boltzmann constant (J/K)

        signal_length = len(signal_from_system.t_signal)

        total_noise = np.zeros(signal_length)

        for noise_name, params in self.noise_parameters.items():
            noise_type = params["type"]
            R = params.get("resistance", 50.0)  # Use individual resistance if defined, else 50

            if noise_type == "quantum":
                T_ns = params.get("T_ns")

                # Bandwidth for quantum noise is 1/tp (from paper)
                # pulse_duration is in seconds but needed in ns
                B_quantum = 1.0 / (self.readout_pulse.pulse_duration * 10 ** 9)

                P_N_quantum = k * T_ns * B_quantum
                sigma = np.sqrt(P_N_quantum * R)

            elif noise_type == "thermal":
                T_eff = params.get("T_eff")
                bandwidth = params.get("bandwidth")

                sigma = np.sqrt(4 * k * T_eff * bandwidth * R)

            else:
                raise ValueError(f"Unknown noise type: {noise_type}")

            # The white noise power spectral density has a unit of dBm.
            total_noise += UnitConverter.dbm_to_amplitude(np.random.normal(0, sigma, size=signal_length))

        return total_noise

    def _IQ_projection_demodulation(self, signal_from_system: Pulse):
        signal_from_system.plot_pulse()

        dt = signal_from_system.t_signal_times[1] - signal_from_system.t_signal_times[0]
        T = signal_from_system.t_signal_times[-1]

        sampling_factor = 1

        is_heterodyne_demodulation = self.readout_pulse.carrier_frequency != self.IQ_projection_frequency
        if is_heterodyne_demodulation:
            f_if = np.abs(self.readout_pulse.carrier_frequency - self.IQ_projection_frequency)

            lowpass_filter_b, lowpass_filter_a = butter(2, f_if * 10, btype="lowpass", fs=1 / dt)

            sampling_factor = int(self.readout_dt / dt)

        # Helper function to be parallelized
        def _process_single_projection():
            noise = self._create_noise(signal_from_system=signal_from_system)

            s = signal_from_system.t_signal.real + noise

            A_lo = 1
            y_I = A_lo / 2 * np.cos(self.IQ_projection_frequency * signal_from_system.t_signal_times)
            y_Q = -A_lo / 2 * np.sin(self.IQ_projection_frequency * signal_from_system.t_signal_times)

            if is_heterodyne_demodulation:
                y_I = filtfilt(lowpass_filter_b, lowpass_filter_a, y_I)
                y_Q = filtfilt(lowpass_filter_b, lowpass_filter_a, y_Q)

            I_pre_integration = s / 2 * y_I * dt
            Q_pre_integration = s / 2 * y_Q * dt

            I_val = 1 / T * np.sum(I_pre_integration[::sampling_factor])
            Q_val = 1 / T * np.sum(Q_pre_integration[::sampling_factor])

            return I_val, Q_val

        # Parallelize the loop
        results = Parallel(n_jobs=-1)(
            delayed(_process_single_projection)()
            for _ in tqdm(range(self.num_iterations))
        )

        # Unpack the results
        I = [res[0] for res in results]
        Q = [res[1] for res in results]

        return I, Q

    @staticmethod
    def _plot_decision_regions(model, scaler, X_data, Y_data, title_suffix=""):
        plt.figure(figsize=(9, 7))

        # Define plot limits based on the provided X_data
        x_min, x_max = X_data[:, 0].min(), X_data[:, 0].max()
        y_min, y_max = X_data[:, 1].min(), X_data[:, 1].max()

        hshift = 0.5 * (x_max - x_min)
        x_min, x_max = x_min - hshift, x_max + hshift
        vshift = 0.5 * (y_max - y_min)
        y_min, y_max = y_min - vshift, y_max + vshift

        # Create a mesh (grid) of points for the decision boundary
        xx, yy = np.meshgrid(np.linspace(x_min, x_max, 100),
                             np.linspace(y_min, y_max, 100),
                             )

        grid_points = np.c_[xx.ravel(), yy.ravel()]

        # Predict the class for each point on the grid using the provided model
        grid_predictions = model.predict(scaler.transform(grid_points)).reshape(xx.shape)

        # Plot the colored decision regions
        plt.imshow(grid_predictions,
                   aspect='auto',
                   alpha=0.3,
                   extent=(x_min, x_max, y_min, y_max),
                   origin='lower',
                   cmap=ListedColormap(['C0', 'C1'])
                   )

        # Plot the actual data points, colored by their state (Y_data)
        plt.scatter(X_data[Y_data == 0, 0], X_data[Y_data == 0, 1], label="|0>", c='C0', alpha=0.8)
        plt.scatter(X_data[Y_data == 1, 0], X_data[Y_data == 1, 1], label="|1>", c='C1', alpha=0.8)

        plt.xlabel("I")
        plt.ylabel("Q")
        plt.title(f"IQ Projection with Logistic Regression Decision Regions {title_suffix}")
        plt.grid(True)
        plt.legend()
        plt.show()

    def _calculate_fidelity(self, I_state_0, Q_state_0, I_state_1, Q_state_1, plot_results: bool = True) -> float:

        # Combine I and Q lists
        X_state_0 = np.column_stack((I_state_0, Q_state_0))
        X_state_1 = np.column_stack((I_state_1, Q_state_1))

        # Create labels (Y) for each state
        Y_state_0 = np.zeros(len(I_state_0), dtype=int)
        Y_state_1 = np.ones(len(I_state_1), dtype=int)

        # Store combined features (X) and true labels (Y) as instance attributes
        X = np.vstack((X_state_0, X_state_1))
        Y = np.hstack((Y_state_0, Y_state_1))

        scaler = StandardScaler()
        X_scaled = scaler.fit_transform(X)  # Scale the full dataset

        # Split the data into 80% training and 20% testing sets
        X_train, X_test, Y_train, Y_test = train_test_split(X_scaled,
                                                            Y,
                                                            test_size=0.2,
                                                            random_state=42
                                                            )

        # Initialize and train a Logistic Regression model on the training data
        model_for_evaluation = SVC(random_state=42)
        model_for_evaluation.fit(X_train, Y_train)

        Y_pred = model_for_evaluation.predict(X_test)

        # Calculate the accuracy
        accuracy = accuracy_score(Y_test, Y_pred)

        # Plot the results if requested, using the model trained on X_train
        if plot_results:
            self._plot_decision_regions(model_for_evaluation, scaler, X, Y, "(Trained on 80% data)")

        return accuracy
