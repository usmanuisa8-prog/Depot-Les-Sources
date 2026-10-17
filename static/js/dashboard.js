/* =========================================
   DEPOT LES SOURCES
   DASHBOARD JAVASCRIPT
========================================= */
document.addEventListener("DOMContentLoaded", function(){
    /* =====================================
       SALES CHART
    ====================================== */
    const chart = document.getElementById("salesChart");
    if(chart){
        new Chart(chart, {
            type:"line",
            data:{
                labels:[
                    "Jan",
                    "Fév",
                    "Mar",
                    "Avr",
                    "Mai",
                    "Juin"
                ],
                datasets:[{
                    label:"Ventes (FCFA)",
                    data:[
                        45000,
                        70000,
                        55000,
                        95000,
                        120000,
                        150000
                    ],
                    tension:0.4,
                    fill:true
                }]
            },
            options:{
                responsive:true,
                plugins:{
                    legend:{
                        display:true
                    }
                },
                scales:{
                    y:{
                        beginAtZero:true,
                        ticks:{
                            callback:function(value){
                                return value + " FCFA";
                            }
                        }
                    }
                }
            }
        });
    }
    /* =====================================
       NOTIFICATION BUTTONS
    ====================================== */
    const notificationButtons =
        document.querySelectorAll(".icon-btn");
    notificationButtons.forEach(button=>{
        button.addEventListener("click",()=>{
            alert("Vous avez de nouvelles notifications");
        });
    });
    /* =====================================
       QUICK ACTION BUTTONS
    ====================================== */
    const actionButtons =
        document.querySelectorAll(".action-card");
    actionButtons.forEach(button=>{
        button.addEventListener("click",function(){
            const action =
            this.querySelector("span").textContent;
            console.log(
                "Action sélectionnée :",
                action
            );
        });
    });
    /* =====================================
       SEARCH SYSTEM
    ====================================== */
    const searchInput =
    document.querySelector(".search-box input");
    if(searchInput){
        searchInput.addEventListener(
            "keyup",
            function(){
                let value =
                this.value.toLowerCase();
                console.log(
                    "Recherche:",
                    value
                );
                // Later:
                // Connect to Flask API
                // Search products/orders/customers
            }
        );
    }
    /* =====================================
       DATE AUTOMATIC UPDATE
    ====================================== */
    const dateElement =
    document.querySelector(".current-date span");
    if(dateElement){
        const today = new Date();
        const options={
            day:"numeric",
            month:"long",
            year:"numeric"
        };
        dateElement.textContent =
        today.toLocaleDateString(
            "fr-FR",
            options
        );
    }
});