# goodwe
GoodWe automatization from linux shell

Here I am sharing my scripts for managing a fotovoltaic power plant I have at home. 
Might be doable with something like Home Assistant, but I thought I'll give it a try and write my own, lightweigt version.
Feel free to reuse or suggest improvement.


Components involved:
- GoodWe invertor (3-phase, 10 kWp)
- Pylontech batteries
- Network relay Web_Relay_Con V2.0 HW-584 to manage water heater

Used git projects:
- https://github.com/marcelblijleven/goodwe - library to communicate with GoodWe invertors
- https://github.com/nielsonm236/NetMod-ServerApp - much better firmware for the network relay than the vendor's original one


The main idea I have is to have a "state vector" with various values stored in a file, and then once in a while (reasonable frequency seems to be 5 minutes) adjust the behaviour based on past and new values.

What I have:
- Read electricity prices from web, disable output to grid for hours where price is <20 Euro/MWh (which is approx. a price where in Czech Rep. becomes selling of the energy non-profitable)
- Enable 1/2/3 phases (2 kW each) of water heater using the LAN relay - currently, it's evaluated every 5 minutes and within fixed hours (cron */5 11-17 * * *), and the amount of phases is based on SOC thresholds (how much is the house battery charged)
- Using the relay to run circulation pump every 20 minutes for 3 minutes
Mind we have also gas water heater. The electrical one is in series in front of the gas one, so if the water is completely heated or pre-heated by the electric one, it then saves the gas costs. For ~6 months of the year, the gas one can be turned off completely.

What I'd like to have:
- The state vector mentioned above, with
    - Reading weather forecast and estimating the remaining energy that will come within a day (ie. sun-hours) - to turn off the water heating in the afternoon/evening so it won't consume energy from battery
    - More intelligent managing of the output to grid, considering the price of the energy and weather forcast (ie. "it will be sunny day, and the energy is expensive in the morning => output to grid, then around 12 price drops => charge battery, then heat water)
- Some alerts/messages to mobile phone (is there some linux>whatsapp library?)
- 
